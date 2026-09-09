# Flash Anzan — Spaced Repetition Trainer

A mental-abacus (flash anzan) practice app: numbers flash on screen one
at a time, you mentally add them, and enter the total. Under the hood,
a spaced-repetition scheduler tracks which digit-addition "patterns"
(friend-of-5, friend-of-10, both) you're weak on, and the number
generator biases what it flashes toward those patterns — so weak spots
come up more often, and get reviewed sooner if you keep missing them.

## Running it

### Desktop (for trying it out / development)
```bash
pip install kivy
python3 main.py
```

### pyDroid3 (Android)
1. In pyDroid, open the **Pip** page and install `kivy` (and `kivy_deps.sdl2`
   / `kivy_deps.glew` if pyDroid prompts for them — on Android these are
   usually bundled automatically).
2. Copy the whole `flashanzan_srs` folder onto the device (file manager,
   cloud drive, USB, etc.) — keep `main.py`, `flashanzan.kv`, and the
   `core/` folder together in the same directory.
3. Open `main.py` in pyDroid and hit **Run**.

Your review progress is saved automatically to a JSON file in the app's
private data directory (`App.user_data_dir`), so it persists between
sessions without any setup.

## How the spaced repetition actually works

The trainer doesn't schedule whole "problems" — it schedules **individual
digit-addition facts**, e.g. `7+8`, `3+4`, `8+6`. There are 90 of them
(existing digit 0–9 × digit being added 1–9), and each one is classified
into a pattern:

| Pattern | Meaning |
|---|---|
| Direct | plain bead push, no trick needed |
| Friend of 5 | need the five-complement trick |
| Friend of 10 | column overflows, carries to the next digit |
| Friend of 5 + 10 | both tricks needed in the same step |

Every fact has its own review interval that:
- **grows** (roughly doubles) each time you get a round right that used it,
- **shrinks hard** the moment you get a round wrong that used it — per your
  request, the scheduler is biased toward *shorter* intervals whenever
  there's doubt, rather than long ones.

When building each round, the generator doesn't pick numbers uniformly at
random. For every digit column it's about to fill in, it looks at all 10
possible digits it could add and weights each one by:
1. How overdue that exact fact is in the scheduler, and
2. A flat multiplier if the resulting pattern is a "hard" one (any
   non-Direct pattern) — controlled by the **"Bias toward hard patterns"**
   slider in Settings.

So even before anything is formally "due," friend-of-5/10 combinations
show up more than plain direct ones; and once a fact is overdue, it shows
up a lot more, until you get it right again.

### Digit-level weak spots (on top of the 90 facts)

The 90 facts are tracked individually, but they're not treated as fully
unrelated to each other. Every fact is also decomposed into its two
digits (`existing` and `add`), and each digit (0-9) separately gets its
own rolling "mastery" score, updated a little on every fact result
regardless of which role that digit played:

- get a fact right → the mastery of *both* digits in it ticks up a bit,
- get one wrong → the mastery of *both* digits ticks down harder (same
  "shrink fast when unsure" philosophy as the per-fact scheduler).

The generator then multiplies each candidate fact's weight by how weak
the learner currently is on its `existing` digit *and* its `add` digit.
So if you keep missing anything with a `7` in it, every fact containing
a 7 gets nudged up — not just the one exact fact you got wrong — because
the weakness is tracked per-digit, not just per-fact. The Stats screen
shows this as a 10-column strip (digits 0-9) colored by current
weakness, above the per-fact table.

This is a layer *on top of* the per-fact SRS, not a replacement for it:
the exact-fact due dates still drive what's due today, while the
digit-level score is an extra multiplicative bias.

## Project layout (why it's split up)

```
flashanzan_srs/
  core/                   <- no Kivy import anywhere in here
    patterns.py           <- classifies a digit-addition into a pattern
    scheduler.py           <- generic spaced-repetition engine + storage
    generator.py            <- builds flashed number sequences, weighted
    session.py               <- glues generator+scheduler into rounds
    _selftest.py              <- plain `python3 -m core._selftest`, no Kivy needed
  main.py                 <- Kivy App + screens (Settings/Flash/Answer/Result/Stats)
  flashanzan.kv           <- Kivy layout/styling
```

`core/` is deliberately UI-agnostic:
- `PatternClassifier` is an abstract interface — `AbacusPatternClassifier`
  is the current implementation, but you could add a different rule set
  (e.g. covering subtraction) without touching anything else.
- `StorageBackend` is likewise abstract — `JSONFileStorage` is used by the
  app, `InMemoryStorage` is used by the self-tests, and you could add a
  database-backed one later with no changes to `SpacedRepetitionScheduler`.
- `NumberSequenceGenerator` only depends on those two interfaces, so it
  doesn't care whether facts are digit-additions or something else.

This means you can run and trust the scheduling/generation logic on its
own, independent of whether Kivy is even installed:
```bash
python3 -m core._selftest
```

## Tuning it

All in Settings, per round:
- **How many numbers per round** — length of the flash sequence.
- **Digits per number** — 1–3 digit terms (more digits = more columns =
  more chances to hit weak patterns per round).
- **Flash speed** — how long each number stays on screen.
- **Bias toward hard patterns** — 1.0x = purely due-ness driven, higher
  values lean harder into friend-of-5/10 combinations regardless of due
  status.

`core/scheduler.py`'s `SchedulerConfig` also exposes the raw numbers if
you want to tune the SRS curve directly: `base_interval_seconds` (the
smallest gap between reviews), `growth` (interval multiplier per correct
rep), `max_rep_cap`, and `wrong_answer_penalty` (how many reps get
knocked off on a miss). For the digit-level layer: `digit_mastery_up` /
`digit_mastery_down` (how fast a digit's mastery rises on a hit vs.
falls on a miss) and `digit_boost` (the max extra multiplier a fully-weak
digit can add to a fact's weight — 0 disables digit-level biasing
entirely).

## What changed vs. the original scripts

The two original snippets (`sr`/spaced-repetition-over-text-files, and
`Saf`/`processor`/pattern-monitoring) had a few rough edges called out in
their own comments (`maxn`'s "original body was broken", `nm`'s "adjust
if wrong"). Rather than preserve those literally, this rewrite keeps the
same *ideas* — exponential-interval spaced repetition, and classifying
column additions by which abacus complement they need — but:
- stores state as JSON instead of `eval()`-ing a text file (eval on
  arbitrary file content is a code-execution risk, and brittle besides),
- classifies patterns using the actual soroban complement algorithm
  (based on spare bead capacity) instead of a before/after digit diff,
- separates "what pattern is this" / "is this fact due" / "what number
  should I flash next" into independent, individually-testable pieces,
- feeds pattern weakness directly into number *generation*, rather than
  the original code's fixed pre-authored item lists.

## NOTE
this code is made with help of claude ai ... THANKS
