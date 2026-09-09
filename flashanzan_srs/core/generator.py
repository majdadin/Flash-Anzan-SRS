"""
generator.py
============

Builds the actual number sequences shown during a Flash Anzan round.

Each term is built digit-by-digit (units first, like an abacus). For
every digit column we look at what digit is currently sitting there
(the running total so far) and pick which digit to *add* to it. That
choice is a weighted random pick over 0-9, where the weight for each
candidate digit comes from two things:

  1. How due/overdue the scheduler considers that exact
     (existing_digit, add_digit) fact (core/scheduler.py).
  2. Whether the resulting pattern is a "hard" one (friend-of-5 /
     friend-of-10) at all -- these get a flat bias multiplier so the
     session leans toward complement patterns rather than plain direct
     addition, even before anything is "due" in the SRS sense.
  3. How weak the learner currently is on each of the two digits
     involved (existing_digit and add_digit) individually, rolled up
     across every fact that digit has ever appeared in -- so missing a
     lot of "7"s nudges every fact with a 7 in it, not just the one
     that was actually missed (core/scheduler.py's digit_multiplier).

A small residual weight is always kept on every candidate so the
sequence never becomes fully deterministic drilling -- there's still
genuine randomness, just skewed toward what needs practice.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import List, Optional, Set

from .patterns import PatternClassifier, PatternType
from .scheduler import SpacedRepetitionScheduler


@dataclass(frozen=True)
class ColumnStep:
    """One column-addition that happened while building the round, tagged
    with *where* it happened so the UI can report practice per digit
    place instead of lumping every column together.

    term_index: which term (0-based) this step belongs to.
    position:   which digit column, 0 = units/1s place, 1 = tens, etc.
    """

    term_index: int
    position: int
    existing: int
    add: int

    @property
    def key(self) -> str:
        return f"{self.existing}+{self.add}"


@dataclass
class RoundData:
    terms: List[int]
    answer: int
    digit_count: int
    touched_keys: Set[str] = field(default_factory=set)
    # Same information as touched_keys, but per-occurrence and tagged with
    # which digit column it happened on -- lets the UI report practice
    # broken down by place (units, tens, ...) instead of only in aggregate.
    steps: List[ColumnStep] = field(default_factory=list)


class NumberSequenceGenerator:
    def __init__(
        self,
        classifier: PatternClassifier,
        scheduler: SpacedRepetitionScheduler,
        bad_pattern_bias: float = 3.0,
        rng: Optional[random.Random] = None,
    ):
        """
        bad_pattern_bias: flat multiplier applied to any non-DIRECT
        pattern's weight, on top of the scheduler's due-ness weight.
        Raise this to see friend-of-5/10 patterns even more often.
        """
        self.classifier = classifier
        self.scheduler = scheduler
        self.bad_pattern_bias = bad_pattern_bias
        self.rng = rng or random.Random()

    def _weight_for(self, existing_digit: int, add_digit: int) -> float:
        key = f"{existing_digit}+{add_digit}"
        w = self.scheduler.weight(key)
        pattern = self.classifier.classify(existing_digit, add_digit)
        if pattern.is_bad_pattern:
            w *= self.bad_pattern_bias
        # Digit-level weakness rollup: on top of this exact fact's own
        # due-ness, lean toward facts that contain a digit the learner has
        # been getting wrong lately, in either role.
        w *= self.scheduler.digit_multiplier(existing_digit)
        w *= self.scheduler.digit_multiplier(add_digit)
        return w

    def _pick_add_digit(self, existing_digit: int, allow_zero: bool) -> int:
        candidates = list(range(0, 10)) if allow_zero else list(range(1, 10))
        weights = [self._weight_for(existing_digit, d) for d in candidates]
        return self.rng.choices(candidates, weights=weights, k=1)[0]

    def generate_round(self, term_count: int = 5, digit_count: int = 1) -> RoundData:
        """Builds `term_count` numbers of up to `digit_count` digits each,
        summed left to right, biasing each column addition toward
        due/weak patterns. Returns the terms, the correct total, and the
        set of DigitFact keys ("existing+add") that were exercised, so
        the caller can feed the result back into the scheduler."""
        running_total = 0
        terms: List[int] = []
        touched: Set[str] = set()
        steps: List[ColumnStep] = []

        for term_index in range(term_count):
            term_value = 0
            top_pos = digit_count - 1
            for pos in range(digit_count):
                existing_digit = (running_total // (10 ** pos)) % 10
                # Never let the term's leading digit be 0 (a "007"-looking
                # flash is confusing); lower digits may be 0 freely.
                allow_zero = pos != top_pos
                add_digit = self._pick_add_digit(existing_digit, allow_zero=allow_zero)
                touched.add(f"{existing_digit}+{add_digit}")
                steps.append(ColumnStep(term_index=term_index, position=pos,
                                         existing=existing_digit, add=add_digit))
                term_value += add_digit * (10 ** pos)
                running_total += add_digit * (10 ** pos)
            terms.append(term_value)

        return RoundData(terms=terms, answer=running_total, digit_count=digit_count,
                          touched_keys=touched, steps=steps)
