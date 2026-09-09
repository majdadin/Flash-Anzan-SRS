"""
scheduler.py
============

A Leitner-style spaced-repetition scheduler with exponentially growing
review intervals, generalized from the original `sr` class:

  * Each tracked item (a `DigitFact` key like "7+8") has a repetition
    count and a "last reviewed" timestamp.
  * Its review interval is `base_seconds * growth ** min(reps, cap)`.
  * An item is "due" once that interval has elapsed since it was last
    reviewed.
  * A correct answer grows the interval (increment reps); a wrong
    answer shrinks it hard (reset reps close to zero) so the item comes
    back around soon — per your request, when in doubt the scheduler
    favors shorter intervals over longer ones.

Storage is behind a small `StorageBackend` interface so state can live
in a JSON file (default, used on desktop/pyDroid), in-memory (for tests),
or something else later (e.g. a database) without touching the
scheduling logic itself.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional


# ---------------------------------------------------------------------------
# Storage abstraction
# ---------------------------------------------------------------------------

class StorageBackend(ABC):
    @abstractmethod
    def load(self) -> dict: ...

    @abstractmethod
    def save(self, data: dict) -> None: ...


class JSONFileStorage(StorageBackend):
    """Persists scheduler state to a JSON file. Replaces the original
    code's `eval()`-on-a-text-file approach, which is both a security
    risk (arbitrary code execution on load) and fragile."""

    def __init__(self, path: str):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f)
        tmp.replace(self.path)


class InMemoryStorage(StorageBackend):
    """Useful for tests/CLI demos where you don't want to touch disk."""

    def __init__(self):
        self._data: dict = {}

    def load(self) -> dict:
        return json.loads(json.dumps(self._data))  # cheap deep copy

    def save(self, data: dict) -> None:
        self._data = json.loads(json.dumps(data))


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

@dataclass
class ReviewState:
    repetitions: int = 0
    last_reviewed: float = 0.0
    introduced: bool = False


@dataclass
class DigitState:
    """Rolled-up weakness tracking for a single digit (0-9), independent
    of which of the 90 facts it showed up in or which role (existing vs.
    add) it played. This sits *alongside* the per-fact ReviewState above
    -- it doesn't replace it -- so that struggling with a digit (e.g.
    consistently missing anything involving a 7) nudges every fact that
    digit appears in, not just the one exact fact you happened to get
    wrong."""

    mastery: float = 0.5   # 0.0 = very weak, 1.0 = fully mastered
    samples: int = 0       # how many results have fed into `mastery`


@dataclass
class PositionState:
    """Plain correct/total tally for a single digit *column* (0 = units,
    1 = tens, ...), independent of which fact was being tested there.
    This is separate from DigitState (which rolls up by digit *value*,
    0-9) -- this rolls up by *place*, so the Stats screen can show e.g.
    'tens column: 40% accurate' regardless of which digits were involved."""

    correct: int = 0
    total: int = 0


@dataclass
class SchedulerConfig:
    base_interval_seconds: float = 60.0   # smallest possible gap between reviews
    growth: float = 1.61803               # interval multiplier per successful rep
    max_rep_cap: int = 15                 # reps beyond this stop growing the interval
    wrong_answer_penalty: int = 2         # reps subtracted on a wrong answer
    new_item_batch: int = 5               # how many new facts to introduce per "catch up" pass
    digit_mastery_up: float = 0.25        # how fast a digit's mastery climbs on a correct answer
    digit_mastery_down: float = 0.40      # how fast it falls on a wrong answer (harsher, same "shrink hard" idea)
    digit_boost: float = 1.5              # max extra multiplier a fully-weak digit adds to a fact's weight


class SpacedRepetitionScheduler:
    """Tracks review state for an arbitrary set of item keys (here:
    `DigitFact.key` strings) and decides which ones are due, how
    urgently, and how to update them after each round."""

    def __init__(self, storage: StorageBackend, config: Optional[SchedulerConfig] = None):
        self.storage = storage
        self.config = config or SchedulerConfig()
        facts_raw, digits_raw, positions_raw = self._split_raw(storage.load())
        self._states: Dict[str, ReviewState] = self._deserialize_facts(facts_raw)
        self._digit_states: Dict[int, DigitState] = self._deserialize_digits(digits_raw)
        self._position_states: Dict[int, PositionState] = self._deserialize_positions(positions_raw)

    # -- persistence -------------------------------------------------------

    @staticmethod
    def _split_raw(raw: dict) -> "tuple[dict, dict, dict]":
        """New storage shape is {"facts": {...}, "digits": {...},
        "positions": {...}}. Older save files (from before digit/position
        tracking existed) are just the flat fact dict, so treat those as
        facts-only with no digit or position history yet."""
        if "facts" in raw or "digits" in raw or "positions" in raw:
            return raw.get("facts", {}), raw.get("digits", {}), raw.get("positions", {})
        return raw, {}, {}

    @staticmethod
    def _deserialize_facts(raw: dict) -> Dict[str, ReviewState]:
        states = {}
        for key, v in raw.items():
            states[key] = ReviewState(
                repetitions=v.get("repetitions", 0),
                last_reviewed=v.get("last_reviewed", 0.0),
                introduced=v.get("introduced", False),
            )
        return states

    @staticmethod
    def _deserialize_digits(raw: dict) -> Dict[int, DigitState]:
        states = {}
        for key, v in raw.items():
            try:
                digit = int(key)
            except (TypeError, ValueError):
                continue
            states[digit] = DigitState(
                mastery=v.get("mastery", 0.5),
                samples=v.get("samples", 0),
            )
        return states

    @staticmethod
    def _deserialize_positions(raw: dict) -> Dict[int, PositionState]:
        states = {}
        for key, v in raw.items():
            try:
                position = int(key)
            except (TypeError, ValueError):
                continue
            states[position] = PositionState(
                correct=v.get("correct", 0),
                total=v.get("total", 0),
            )
        return states

    def _persist(self) -> None:
        facts_raw = {
            key: {
                "repetitions": s.repetitions,
                "last_reviewed": s.last_reviewed,
                "introduced": s.introduced,
            }
            for key, s in self._states.items()
        }
        digits_raw = {
            str(digit): {"mastery": s.mastery, "samples": s.samples}
            for digit, s in self._digit_states.items()
        }
        positions_raw = {
            str(position): {"correct": s.correct, "total": s.total}
            for position, s in self._position_states.items()
        }
        self.storage.save({"facts": facts_raw, "digits": digits_raw, "positions": positions_raw})

    # -- interval math -------------------------------------------------------

    def _interval_seconds(self, reps: int) -> float:
        capped = min(max(reps, 0), self.config.max_rep_cap)
        return self.config.base_interval_seconds * (self.config.growth ** capped)

    def _state_for(self, key: str) -> ReviewState:
        return self._states.setdefault(key, ReviewState())

    def ensure_catalog(self, keys: Iterable[str]) -> None:
        """Make sure every key from the pattern catalog has a state entry
        (uninitialized/not-yet-introduced) so lookups never KeyError."""
        for key in keys:
            self._states.setdefault(key, ReviewState())

    # -- querying -------------------------------------------------------

    def is_due(self, key: str, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        state = self._states.get(key)
        if state is None or not state.introduced:
            return False
        wait = self._interval_seconds(state.repetitions)
        return (now - state.last_reviewed) >= wait

    def due_items(self, now: Optional[float] = None) -> List[str]:
        now = now if now is not None else time.time()
        return [k for k in self._states if self.is_due(k, now)]

    def seconds_until_due(self, key: str, now: Optional[float] = None) -> Optional[float]:
        """None if already due (or unseen); otherwise seconds remaining."""
        now = now if now is not None else time.time()
        state = self._states.get(key)
        if state is None or not state.introduced:
            return None
        wait = self._interval_seconds(state.repetitions)
        remaining = wait - (now - state.last_reviewed)
        return max(remaining, 0.0) if remaining > 0 else None

    def weight(self, key: str, now: Optional[float] = None) -> float:
        """A relative 'how much does this fact need practice right now'
        score, used by the generator to bias number selection. Not-yet-
        introduced items get a modest constant weight (so they eventually
        show up); overdue items get a weight that grows with how overdue
        they are; comfortably-not-due items get a small residual weight
        so the session doesn't become 100% predictable drilling."""
        now = now if now is not None else time.time()
        state = self._states.get(key)
        if state is None or not state.introduced:
            return 1.0
        wait = self._interval_seconds(state.repetitions)
        elapsed = now - state.last_reviewed
        overdue = elapsed - wait
        if overdue >= 0:
            overdue_ratio = overdue / wait if wait > 0 else overdue
            return 3.0 + min(overdue_ratio, 5.0) * 4.0  # ranges roughly 3..23
        return 0.5

    # -- recording results -------------------------------------------------------

    def introduce(self, key: str, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        state = self._state_for(key)
        if not state.introduced:
            state.introduced = True
            state.repetitions = 0
            state.last_reviewed = now
            self._persist()

    def introduce_batch(self, keys: Iterable[str], now: Optional[float] = None) -> List[str]:
        """Introduce up to `new_item_batch` not-yet-seen keys from the
        given ordered list of candidates. Returns the keys introduced."""
        now = now if now is not None else time.time()
        introduced = []
        for key in keys:
            if len(introduced) >= self.config.new_item_batch:
                break
            state = self._states.get(key)
            if state is None or not state.introduced:
                self.introduce(key, now)
                introduced.append(key)
        return introduced

    def record_result(self, key: str, correct: bool, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        state = self._state_for(key)
        state.introduced = True
        if correct:
            state.repetitions = min(state.repetitions + 1, self.config.max_rep_cap)
        else:
            # Shrink the interval hard on a miss -- bring it back around soon
            # rather than merely trimming it, per "shorter is better when unsure".
            state.repetitions = max(state.repetitions - self.config.wrong_answer_penalty, 0)
        state.last_reviewed = now
        self._record_digits_from_key(key, correct)
        self._persist()

    def record_position_result(self, position: int, correct: bool, now: Optional[float] = None) -> None:
        """Tally a single column-place (units=0, tens=1, ...) result,
        independent of which fact was being tested there. Used purely for
        reporting -- e.g. the Stats screen showing 'tens column: 40%
        accurate' -- and doesn't feed into scheduling/weighting."""
        state = self._position_states.setdefault(position, PositionState())
        state.total += 1
        if correct:
            state.correct += 1
        self._persist()

    def position_stats(self) -> Dict[int, dict]:
        """Per-column (0=units, 1=tens, ...) accuracy rollup for UI/
        introspection. Only columns with at least one recorded result are
        included."""
        out = {}
        for position, state in sorted(self._position_states.items()):
            accuracy = (state.correct / state.total) if state.total else None
            out[position] = {"correct": state.correct, "total": state.total, "accuracy": accuracy}
        return out

    # -- digit-level rollup -------------------------------------------------------
    #
    # Every fact key is "existing+add" (e.g. "7+8"). Whenever a fact's result
    # comes in, both digits it's built from get nudged too -- regardless of
    # which role (existing vs. add) they played -- so weakness on a specific
    # digit shows up across every fact that digit appears in, not just the
    # one fact that was actually asked.

    def _record_digits_from_key(self, key: str, correct: bool) -> None:
        try:
            existing_s, add_s = key.split("+")
            digits = {int(existing_s), int(add_s)}
        except (ValueError, AttributeError):
            return
        for digit in digits:
            if 0 <= digit <= 9:
                self._record_digit_result(digit, correct)

    def _digit_state_for(self, digit: int) -> DigitState:
        return self._digit_states.setdefault(digit, DigitState())

    def _record_digit_result(self, digit: int, correct: bool) -> None:
        state = self._digit_state_for(digit)
        if correct:
            state.mastery += self.config.digit_mastery_up * (1.0 - state.mastery)
        else:
            state.mastery -= self.config.digit_mastery_down * state.mastery
        state.mastery = min(1.0, max(0.0, state.mastery))
        state.samples += 1

    def digit_weakness(self, digit: int) -> float:
        """0.0 = fully mastered, 1.0 = maximally weak. Digits with no
        recorded results yet sit at a neutral 0.5."""
        state = self._digit_states.get(digit)
        if state is None or state.samples == 0:
            return 0.5
        return 1.0 - state.mastery

    def digit_multiplier(self, digit: int) -> float:
        """Weight multiplier the generator applies for a digit appearing
        in a candidate fact -- 1.0x for a mastered digit, up to
        1.0 + digit_boost for a maximally weak one."""
        return 1.0 + self.config.digit_boost * self.digit_weakness(digit)

    def digit_stats(self) -> Dict[int, dict]:
        """Per-digit (0-9) rollup for UI/introspection."""
        out = {}
        for digit in range(10):
            state = self._digit_states.get(digit, DigitState())
            out[digit] = {
                "mastery": state.mastery,
                "samples": state.samples,
                "weakness": self.digit_weakness(digit),
            }
        return out

    def record_results(self, keys: Iterable[str], correct: bool, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        for key in keys:
            self.record_result(key, correct, now)

    # -- introspection / stats -------------------------------------------------------

    def stats(self) -> Dict[str, dict]:
        now = time.time()
        out = {}
        for key, state in self._states.items():
            out[key] = {
                "repetitions": state.repetitions,
                "introduced": state.introduced,
                "due": self.is_due(key, now),
                "seconds_until_due": self.seconds_until_due(key, now),
            }
        return out
