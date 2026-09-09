"""
patterns.py
===========

Flash Anzan (soroban / mental abacus) practice is built from single-digit
"column additions": you already have a digit sitting on a column and you
add another digit to it. Depending on the two digits, you either push
beads directly, or you need a "complement" trick:

  * DIRECT           - no trick needed, beads are just pushed.
  * SMALL_FRIEND      - "friend of 5" complement: you have to trade the
                         5-bead for lower beads (e.g. 3 + 4).
  * BIG_FRIEND        - "friend of 10" complement: the column overflows
                         and carries into the next column (e.g. 7 + 8).
  * SMALL_AND_BIG     - both tricks are needed in the same step
                         (e.g. 8 + 6).

DIRECT steps are "easy"/random-feeling; the three complement patterns are
the ones people actually get wrong, so the generator (generator.py)
leans on this classification to pick which digit combinations to flash
more often, and the scheduler (scheduler.py) tracks each individual
(existing_digit, add_digit) fact as its own spaced-repetition item.

This module only *classifies* — it has no notion of timing, scheduling,
or UI. That separation is the "abstraction" the rest of the app is built
on: swap AbacusPatternClassifier for a different rule set (e.g. also
covering subtraction) without touching scheduler/generator/UI code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import List, Tuple


class PatternType(Enum):
    DIRECT = "direct"
    SMALL_FRIEND = "small_friend"          # friend-of-5 complement
    BIG_FRIEND = "big_friend"              # friend-of-10 complement (carry)
    SMALL_AND_BIG_FRIEND = "small_and_big_friend"

    @property
    def label(self) -> str:
        return {
            PatternType.DIRECT: "Direct",
            PatternType.SMALL_FRIEND: "Friend of 5",
            PatternType.BIG_FRIEND: "Friend of 10",
            PatternType.SMALL_AND_BIG_FRIEND: "Friend of 5 + 10",
        }[self]

    @property
    def is_bad_pattern(self) -> bool:
        """Anything that isn't a plain direct push counts as a 'hard' pattern."""
        return self is not PatternType.DIRECT


@dataclass(frozen=True)
class DigitFact:
    """One column-addition fact: `existing` digit already on the column,
    plus `add` digit being added to it. This is the atomic unit the
    spaced-repetition scheduler tracks."""

    existing: int  # 0-9
    add: int       # 0-9 (0 is allowed as a "no-op" filler digit)

    @property
    def key(self) -> str:
        return f"{self.existing}+{self.add}"

    @property
    def total(self) -> int:
        return self.existing + self.add


class PatternClassifier(ABC):
    """Interface so the classification rules can be swapped out later
    (e.g. a subtraction variant, or a two-digit-friend variant) without
    changing generator.py or scheduler.py."""

    @abstractmethod
    def classify(self, existing: int, add: int) -> PatternType: ...

    @abstractmethod
    def catalog(self) -> List[DigitFact]:
        """Every fact this classifier knows about, used to seed the scheduler."""
        ...


class AbacusPatternClassifier(PatternClassifier):
    """Classifies a single-column addition by comparing the abacus bead
    layout before and after the addition.

    A digit `d` is represented on the abacus as one "upper" 5-bead
    (`d // 5`, 0 or 1) and up to four "lower" 1-beads (`d % 5`).
    """

    def classify(self, existing: int, add: int) -> PatternType:
        """Mirrors the standard soroban single-column addition algorithm:
        the technique used depends on how much *spare bead capacity* the
        existing digit has, not just the before/after digit values.

        existing = 5*existing_upper + existing_lower (existing_lower 0-4)
        """
        existing_lower = existing % 5
        existing_upper = existing // 5

        if add < 5:
            # `add` has no 5-bead of its own; can it be pushed directly?
            if existing_lower + add <= 4:
                return PatternType.DIRECT
            # Not enough spare lower beads -> need the five-complement.
            if existing_upper == 0:
                return PatternType.SMALL_FRIEND
            # Five-complement would need the column's 5-bead, which is
            # already in use -> cascades into a ten-complement too.
            return PatternType.SMALL_AND_BIG_FRIEND

        # add >= 5: it carries its own 5-bead plus a 0-4 remainder.
        add_lower = add - 5
        if existing_upper == 0 and existing_lower + add_lower <= 4:
            return PatternType.DIRECT
        # Ten-complement needed: subtract (10 - add) from existing_lower.
        complement = 10 - add
        if existing_lower >= complement:
            return PatternType.BIG_FRIEND
        # Not enough spare lower beads to pay the ten-complement either
        # -> both complements are needed in the same step.
        return PatternType.SMALL_AND_BIG_FRIEND

    def catalog(self) -> List[DigitFact]:
        facts = []
        for existing in range(0, 10):
            for add in range(1, 10):  # add=0 is a meaningless drill fact
                facts.append(DigitFact(existing, add))
        return facts


def build_default_catalog() -> Tuple[AbacusPatternClassifier, List[DigitFact]]:
    classifier = AbacusPatternClassifier()
    return classifier, classifier.catalog()
