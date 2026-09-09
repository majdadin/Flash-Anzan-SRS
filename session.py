"""
session.py
==========

Thin orchestration layer: pulls the two building blocks (generator +
scheduler) together into "start a round" / "grade this round" calls the
UI layer can use without knowing anything about SRS internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .generator import NumberSequenceGenerator, RoundData
from .patterns import PatternClassifier
from .scheduler import SpacedRepetitionScheduler


@dataclass
class SessionSettings:
    term_count: int = 5
    digit_count: int = 1
    flash_interval_ms: int = 800


class FlashAnzanSession:
    def __init__(
        self,
        classifier: PatternClassifier,
        scheduler: SpacedRepetitionScheduler,
        generator: Optional[NumberSequenceGenerator] = None,
    ):
        self.classifier = classifier
        self.scheduler = scheduler
        self.generator = generator or NumberSequenceGenerator(classifier, scheduler)
        # Make sure the scheduler knows about every fact so weighting works
        # even before anything has been formally "introduced".
        self.scheduler.ensure_catalog(f.key for f in classifier.catalog())
        self._current_round: Optional[RoundData] = None

    def start_round(self, settings: SessionSettings) -> RoundData:
        # Bring a few new facts into rotation each round, oldest/lowest
        # priority catalog entries first, so the whole fact table
        # eventually gets scheduled rather than only ever touching
        # facts the generator happens to roll.
        all_keys = [f.key for f in self.classifier.catalog()]
        self.scheduler.introduce_batch(all_keys)

        round_data = self.generator.generate_round(
            term_count=settings.term_count,
            digit_count=settings.digit_count,
        )
        self._current_round = round_data
        return round_data

    def submit_answer(self, user_answer: int) -> bool:
        if self._current_round is None:
            raise RuntimeError("start_round() must be called before submit_answer()")
        round_data = self._current_round
        correct = user_answer == round_data.answer

        # Grade column-by-column instead of all-or-nothing: a slip in the
        # tens column shouldn't punish the SRS state of facts that only
        # ever touched the units column (and vice versa). Each step
        # already knows which digit column it happened on, so look up
        # whether *that* column matched and record just that fact's
        # result -- not every fact touched anywhere in the round.
        digit_correct = self._digit_correctness(user_answer, round_data.answer, round_data.digit_count)
        for step in round_data.steps:
            step_correct = digit_correct.get(step.position, False)
            self.scheduler.record_result(step.key, step_correct)
            self.scheduler.record_position_result(step.position, step_correct)

        return correct

    @staticmethod
    def _digit_correctness(user_answer: int, true_answer: int, digit_count: int) -> dict:
        """Compare the user's answer to the true answer one column at a
        time (units, tens, ...) so each column's correctness can be
        judged independently. A negative/garbage answer just means every
        column is wrong."""
        if user_answer < 0 or true_answer < 0:
            return {pos: False for pos in range(max(digit_count, 1))}
        # Cover every column the round touched, plus any extra columns a
        # carry pushed the true total (or the user's guess) into.
        num_positions = max(digit_count, len(str(true_answer)), len(str(user_answer)))
        return {
            pos: (user_answer // (10 ** pos)) % 10 == (true_answer // (10 ** pos)) % 10
            for pos in range(num_positions)
        }

    @property
    def current_round(self) -> Optional[RoundData]:
        return self._current_round
