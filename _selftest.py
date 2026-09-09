"""Not part of the app - just a sanity check runnable with plain python3."""
import random
import time

from .generator import NumberSequenceGenerator
from .patterns import AbacusPatternClassifier, PatternType
from .scheduler import InMemoryStorage, SchedulerConfig, SpacedRepetitionScheduler
from .session import FlashAnzanSession, SessionSettings


def test_classifier_examples():
    c = AbacusPatternClassifier()
    assert c.classify(1, 1) == PatternType.DIRECT          # 1+1=2, no tricks
    assert c.classify(3, 4) == PatternType.SMALL_FRIEND     # 3+4=7 -> 5-bead flips, no carry
    assert c.classify(7, 8) == PatternType.BIG_FRIEND       # 7+8=15 -> carries
    assert c.classify(8, 6) == PatternType.SMALL_AND_BIG_FRIEND  # 8+6=14
    print("classifier examples OK")


def test_scheduler_due_logic():
    storage = InMemoryStorage()
    cfg = SchedulerConfig(base_interval_seconds=10, growth=2.0)
    sched = SpacedRepetitionScheduler(storage, cfg)
    now = 1000.0
    sched.introduce("7+8", now=now)
    assert not sched.is_due("7+8", now=now + 5)     # 5s < 10s interval
    assert sched.is_due("7+8", now=now + 11)         # past interval -> due

    sched.record_result("7+8", correct=True, now=now + 11)
    # interval should have grown (reps=1 -> 10*2=20s)
    assert not sched.is_due("7+8", now=now + 11 + 15)
    assert sched.is_due("7+8", now=now + 11 + 21)

    sched.record_result("7+8", correct=False, now=now + 11 + 21)
    # wrong answer should shrink reps back toward 0 -> short interval again
    assert sched.is_due("7+8", now=now + 11 + 21 + 11)
    print("scheduler due/interval logic OK")


def test_generator_biases_toward_weak_patterns():
    storage = InMemoryStorage()
    sched = SpacedRepetitionScheduler(storage, SchedulerConfig(base_interval_seconds=1))
    classifier = AbacusPatternClassifier()
    sched.ensure_catalog(f.key for f in classifier.catalog())

    rng = random.Random(42)
    gen = NumberSequenceGenerator(classifier, sched, bad_pattern_bias=5.0, rng=rng)

    pattern_counts = {p: 0 for p in PatternType}
    trials = 4000
    for _ in range(trials):
        existing = rng.randint(0, 9)
        add = gen._pick_add_digit(existing, allow_zero=True)
        pattern_counts[classifier.classify(existing, add)] += 1

    direct_ratio = pattern_counts[PatternType.DIRECT] / trials
    print("pattern distribution (biased):", pattern_counts, "direct ratio:", direct_ratio)
    # With bad_pattern_bias=5 the DIRECT share should be well under half
    # (a uniform random baseline would put DIRECT around ~50-55%).
    assert direct_ratio < 0.40, f"expected DIRECT to be suppressed, got {direct_ratio}"
    print("generator bias OK")


def test_generator_favors_due_items_over_not_due():
    storage = InMemoryStorage()
    sched = SpacedRepetitionScheduler(storage, SchedulerConfig(base_interval_seconds=1000))
    classifier = AbacusPatternClassifier()
    sched.ensure_catalog(f.key for f in classifier.catalog())

    # Introduce two DIRECT facts (so pattern-type bias is equal) and make
    # one of them overdue, the other freshly reviewed (not due).
    now = time.time()
    sched.introduce("2+2", now=now - 100000)      # long overdue
    sched.introduce("1+1", now=now)                # just reviewed, not due

    rng = random.Random(7)
    gen = NumberSequenceGenerator(classifier, sched, bad_pattern_bias=1.0, rng=rng)
    w_due = gen._weight_for(2, 2)
    w_not_due = gen._weight_for(1, 1)
    print("weight overdue:", w_due, "weight not-due:", w_not_due)
    assert w_due > w_not_due
    print("due-item weighting OK")


def test_digit_rollup_boosts_related_facts():
    storage = InMemoryStorage()
    sched = SpacedRepetitionScheduler(storage, SchedulerConfig(base_interval_seconds=1000))
    classifier = AbacusPatternClassifier()
    sched.ensure_catalog(f.key for f in classifier.catalog())

    now = time.time()
    # Introduce two DIRECT facts that don't share any digits, both at the
    # same not-due state, so per-fact weight and pattern bias are equal.
    sched.introduce("1+1", now=now)
    sched.introduce("2+2", now=now)

    rng = random.Random(3)
    gen = NumberSequenceGenerator(classifier, sched, bad_pattern_bias=1.0, rng=rng)
    w_before_1 = gen._weight_for(1, 1)
    w_before_2 = gen._weight_for(2, 2)
    assert abs(w_before_1 - w_before_2) < 1e-9  # symmetric before any misses

    # Miss digit "1" a bunch of times via an unrelated fact ("1+3") --
    # this should NOT touch "2+2" but SHOULD boost "1+1" even though
    # "1+1" itself was never answered wrong.
    for _ in range(6):
        sched.record_result("1+3", correct=False, now=now)

    w_after_1 = gen._weight_for(1, 1)
    w_after_2 = gen._weight_for(2, 2)
    print("weight for digit-1 fact after missing 1s:", w_after_1,
          "vs untouched digit-2 fact:", w_after_2)
    assert w_after_1 > w_before_1          # "1+1" got boosted by digit rollup
    assert abs(w_after_2 - w_before_2) < 1e-9  # "2+2" is untouched
    assert w_after_1 > w_after_2
    print("digit rollup OK")


def test_full_session_round():
    storage = InMemoryStorage()
    sched = SpacedRepetitionScheduler(storage, SchedulerConfig(base_interval_seconds=1))
    classifier = AbacusPatternClassifier()
    session = FlashAnzanSession(classifier, sched)

    round_data = session.start_round(SessionSettings(term_count=5, digit_count=2))
    assert len(round_data.terms) == 5
    assert round_data.answer == sum(round_data.terms)
    correct = session.submit_answer(round_data.answer)
    assert correct is True
    print("full session round OK ->", round_data.terms, "=", round_data.answer)


if __name__ == "__main__":
    test_classifier_examples()
    test_scheduler_due_logic()
    test_generator_biases_toward_weak_patterns()
    test_generator_favors_due_items_over_not_due()
    test_digit_rollup_boosts_related_facts()
    test_full_session_round()
    print("\nALL SELF-TESTS PASSED")
