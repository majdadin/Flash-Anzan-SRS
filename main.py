"""
main.py
=======

Kivy front-end for the Flash Anzan spaced-repetition trainer.

This file only handles screens/widgets/timers. All the actual logic
(pattern classification, scheduling, number generation) lives in
core/ and has no idea Kivy exists -- see core/__init__.py for why.

Run on desktop:
    pip install kivy
    python3 main.py

Run on pyDroid3 (Android):
    1. Install the "kivy" package from pyDroid's pip page (Menu -> Pip).
    2. Copy this whole `flashanzan_srs` folder onto the device
       (e.g. via a file manager, or `Get Content` in pyDroid).
    3. Open main.py in pyDroid and press Run.
    Progress is saved automatically between sessions in the app's
    private data directory.
"""

import os
import sys
import time

# Make sure `core` is importable regardless of the working directory
# pyDroid/desktop launches this script from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kivy.app import App
from kivy.clock import Clock
from kivy.factory import Factory
from kivy.properties import StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.screenmanager import Screen, ScreenManager, SlideTransition

from core.patterns import AbacusPatternClassifier
from core.scheduler import JSONFileStorage, SchedulerConfig, SpacedRepetitionScheduler
from core.generator import NumberSequenceGenerator
from core.session import FlashAnzanSession, SessionSettings


KV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flashanzan.kv")
# NOTE: do NOT also Builder.load_file(KV_PATH) in build() below. Kivy's App
# base class auto-loads a kv file named after the app class (FlashAnzanApp
# -> "flashanzan.kv") from this same directory, automatically, before
# build() runs. Since our kv file happens to match that exact naming
# convention, calling Builder.load_file() again here loaded every rule a
# second time -- which is why every screen's widgets (sliders, buttons,
# labels...) were being built twice, stacked one after another.


# A handful of hues from the "Open Color" palette (open-source, MIT
# licensed set of colors -- https://yeun.github.io/open-color/), grouped
# by how many digits a number has. The color is chosen by *which digit
# group the number belongs to*, not at random -- so every 2-digit number
# always draws from the same family, every 3-digit number from another,
# etc. Within a group we rotate through its colors in a fixed order,
# which is what keeps two numbers in a row from ever landing on the same
# color even if they happen to share a digit count.
OPEN_COLOR_GROUPS = {
    1: ("fa5252", "fd7e14", "fab005"),   # red, orange, yellow
    2: ("339af0", "22b8cf", "20c997"),   # blue, cyan, teal
    3: ("be4bdb", "7950f2", "e64980"),   # grape, violet, pink
}
OPEN_COLOR_FALLBACK = ("40c057", "82c91e", "4c6ef5")  # green, lime, indigo


def _hex_to_rgba(hex_code: str):
    hex_code = hex_code.lstrip("#")
    r = int(hex_code[0:2], 16) / 255.0
    g = int(hex_code[2:4], 16) / 255.0
    b = int(hex_code[4:6], 16) / 255.0
    return (r, g, b, 1)


class RootScreen(Screen):
    pass


class SettingsScreen(RootScreen):
    # (default, min, max) clamp bounds -- text inputs let the user type
    # anything, so we still clamp to values the rest of the app can
    # actually handle instead of trusting raw input.
    _TERM_BOUNDS = (5, 2, 50)
    _DIGIT_BOUNDS = (1, 1, 6)
    _SPEED_BOUNDS = (800, 100, 10000)
    _BIAS_BOUNDS = (3.0, 1.0, 20.0)

    @staticmethod
    def _parse_clamped(text, bounds, cast):
        default, lo, hi = bounds
        try:
            value = cast(text.strip())
        except (ValueError, AttributeError):
            return default
        return max(lo, min(hi, value))

    def start_round(self):
        app = App.get_running_app()
        term_count = self._parse_clamped(self.ids.term_input.text, self._TERM_BOUNDS, int)
        digit_count = self._parse_clamped(self.ids.digit_input.text, self._DIGIT_BOUNDS, int)
        speed_ms = self._parse_clamped(self.ids.speed_input.text, self._SPEED_BOUNDS, int)
        bias = self._parse_clamped(self.ids.bias_input.text, self._BIAS_BOUNDS, float)

        # Reflect back whatever was actually used (in case the raw text
        # was invalid/out of range and got clamped/defaulted).
        self.ids.term_input.text = str(term_count)
        self.ids.digit_input.text = str(digit_count)
        self.ids.speed_input.text = str(speed_ms)
        self.ids.bias_input.text = str(bias)

        app.session.generator.bad_pattern_bias = bias
        settings = SessionSettings(
            term_count=term_count,
            digit_count=digit_count,
            flash_interval_ms=speed_ms,
        )
        app.begin_round(settings)


class FlashScreen(RootScreen):
    # Kept as a plain attribute (not a Kivy Property) -- it just holds a
    # reference to whatever Clock event is currently pending so we can
    # cancel it. Without this, re-entering the screen (starting a new
    # round, or backing out and back in) left the old schedule_once chain
    # running *alongside* the new one, so numbers flashed twice on top of
    # each other and the progress counter jumped by twos.
    _pending_event = None
    # Separate handle for the "reveal the number after the gap" event
    # (see _show_next). Tracked independently of _pending_event because
    # the two can be in flight at the same time -- one waiting to blank
    # the label, the other already counting down to the *next* number --
    # and both need to be cancelled if the round is stopped mid-gap.
    _pending_reveal_event = None

    # Blank gap shown between two numbers so consecutive digits read as
    # separate beats instead of running together, without any blink/
    # flicker. Tuned so the gap:
    #   - stays perceptible even when the flash interval is fast
    #     (floor at _MIN_GAP_S), and
    #   - never eats meaningfully into the time available to actually
    #     read the number when the interval is very short (capped both
    #     in absolute terms via _MAX_GAP_S, and relatively via
    #     _MAX_GAP_INTERVAL_FRACTION so it's always a minority of the
    #     interval, even at the fastest configurable speed).
    _GAP_INTERVAL_FRACTION = 0.15
    _MIN_GAP_S = 0.03
    _MAX_GAP_S = 0.09
    _MAX_GAP_INTERVAL_FRACTION = 0.4

    def on_enter(self):
        self._cancel_pending()
        app = App.get_running_app()
        self._terms = list(app.current_round.terms)
        self._index = 0
        self._interval_s = app.current_settings.flash_interval_ms / 1000.0
        self.ids.flash_label.text = ""
        self.ids.flash_label.opacity = 1
        self.ids.progress_label.text = ""
        # Per-digit-group rotation counters and the last color shown,
        # reset every round so color assignment is deterministic and
        # repeatable, not random.
        self._color_cursor = {}
        self._last_color = None
        # brief "get ready" beat before the first number
        self._pending_event = Clock.schedule_once(self._show_next, 0.6)

    def on_leave(self):
        # Whether we left because the round finished, or because the
        # user hit Stop / navigated away mid-round, make sure nothing
        # keeps ticking in the background.
        self._cancel_pending()

    def _cancel_pending(self):
        if self._pending_event is not None:
            self._pending_event.cancel()
            self._pending_event = None
        if self._pending_reveal_event is not None:
            self._pending_reveal_event.cancel()
            self._pending_reveal_event = None

    def _color_for_term(self, term: int):
        """Pick a color for `term` based on its digit count (1-digit,
        2-digit, 3-digit, ...), rotating through that group's colors in
        a fixed order so the assignment is deterministic rather than
        random, and never repeating the immediately previous color."""
        digits = len(str(abs(int(term))))
        group = OPEN_COLOR_GROUPS.get(digits, OPEN_COLOR_FALLBACK)
        cursor = self._color_cursor.get(digits, 0)
        color = _hex_to_rgba(group[cursor % len(group)])
        cursor += 1
        if color == self._last_color:
            color = _hex_to_rgba(group[cursor % len(group)])
            cursor += 1
        self._color_cursor[digits] = cursor
        self._last_color = color
        return color

    def _show_next(self, dt):
        if self._index >= len(self._terms):
            self.ids.flash_label.text = ""
            self._pending_event = None
            App.get_running_app().go_to_answer()
            return

        label = self.ids.flash_label
        term = self._terms[self._index]
        color = self._color_for_term(term)
        self.ids.progress_label.text = f"{self._index + 1} / {len(self._terms)}"

        # Blank the label for a short, fixed gap -- no opacity animation,
        # so there's no flicker/blink -- then reveal the number. That gap
        # is what separates this number from the previous one; it's sized
        # relative to the flash interval (with a floor and a ceiling) so
        # it's still noticeable at fast speeds without swallowing the
        # time actually needed to read the number.
        if self._pending_reveal_event is not None:
            self._pending_reveal_event.cancel()
            self._pending_reveal_event = None
        label.opacity = 1
        label.text = ""

        gap_s = max(self._MIN_GAP_S, min(self._MAX_GAP_S, self._interval_s * self._GAP_INTERVAL_FRACTION))
        gap_s = min(gap_s, self._interval_s * self._MAX_GAP_INTERVAL_FRACTION)

        def _reveal(dt2, term=term, color=color):
            self._pending_reveal_event = None
            label.text = str(term)
            label.color = color

        self._pending_reveal_event = Clock.schedule_once(_reveal, gap_s)

        self._index += 1
        self._pending_event = Clock.schedule_once(self._show_next, self._interval_s)

    def stop_round(self):
        self._cancel_pending()
        App.get_running_app().stop_round()


class AnswerScreen(RootScreen):
    def on_enter(self):
        self.ids.answer_input.text = ""
        Clock.schedule_once(lambda dt: setattr(self.ids.answer_input, "focus", True), 0.1)

    def submit(self):
        raw = self.ids.answer_input.text.strip()
        if raw == "":
            return
        try:
            value = int(raw)
        except ValueError:
            return
        App.get_running_app().grade_answer(value)


class ResultScreen(RootScreen):
    def on_enter(self):
        app = App.get_running_app()
        correct = app.last_result_correct
        round_data = app.current_round

        if correct:
            self.ids.result_label.text = "Correct!"
            self.ids.result_label.color = (0.35, 0.85, 0.45, 1)
        else:
            self.ids.result_label.text = "Not quite"
            self.ids.result_label.color = (0.9, 0.35, 0.35, 1)

        self.ids.detail_label.text = (
            f"{'  +  '.join(str(t) for t in round_data.terms)}  =  {round_data.answer}\n"
            f"Your answer: {app.last_user_answer}"
        )

        pattern_summary = app.summarize_round_patterns(round_data)
        self.ids.pattern_label.text = pattern_summary

    def next_round(self):
        App.get_running_app().begin_round(App.get_running_app().current_settings)


class StatsScreen(RootScreen):
    def on_enter(self):
        app = App.get_running_app()
        from kivy.uix.label import Label

        # -- digit weak-spot strip: one column per digit 0-9, colored by
        # how weak the learner currently is on that digit (rolled up
        # across every fact it appears in, not just one). --------------
        digit_grid = self.ids.digit_grid
        digit_grid.clear_widgets()
        digit_stats = app.scheduler.digit_stats()
        for digit in range(10):
            info = digit_stats[digit]
            weakness = info["weakness"]
            # green (mastered) -> amber -> red (weak); grey if never seen
            if info["samples"] == 0:
                color = (0.55, 0.55, 0.6, 1)
            else:
                color = (0.5 + weakness * 0.4, 0.75 - weakness * 0.45, 0.4, 1)
            box = BoxLayout(orientation="vertical")
            box.add_widget(Label(text=f"[b]{digit}[/b]", markup=True, color=color))
            pct_text = f"{int(weakness * 100)}%" if info["samples"] else "—"
            box.add_widget(Label(text=pct_text, color=color, font_size="12sp"))
            digit_grid.add_widget(box)

        # -- column accuracy strip: one entry per digit *place* (units,
        # tens, ...) showing how often you get that column right,
        # regardless of which digits were involved. Only columns with at
        # least one recorded result show up. ----------------------------
        position_grid = self.ids.position_grid
        position_grid.clear_widgets()
        place_names = {0: "1s", 1: "10s", 2: "100s", 3: "1000s", 4: "10k", 5: "100k", 6: "1M"}
        position_stats = app.scheduler.position_stats()
        for position, info in position_stats.items():
            label = place_names.get(position, f"10^{position}")
            box = BoxLayout(orientation="vertical")
            if info["total"] == 0:
                color = (0.55, 0.55, 0.6, 1)
                pct_text = "—"
            else:
                accuracy = info["accuracy"]
                # green (accurate) -> amber -> red (inaccurate)
                color = (0.9 - accuracy * 0.4, 0.4 + accuracy * 0.45, 0.4, 1)
                pct_text = f"{int(accuracy * 100)}%"
            box.add_widget(Label(text=f"[b]{label}[/b]", markup=True, color=color))
            box.add_widget(Label(text=pct_text, color=color, font_size="12sp"))
            position_grid.add_widget(box)

        grid = self.ids.stats_grid
        grid.clear_widgets()

        headers = ["Fact", "Pattern", "Reps", "Status"]
        for h in headers:
            grid.add_widget(Label(text=f"[b]{h}[/b]", markup=True, color=(0.8, 0.8, 0.9, 1),
                                   size_hint_y=None, height=28))

        classifier = app.classifier
        stats = app.scheduler.stats()
        now = time.time()

        # Sort: due first, then by soonest to come due, then alphabetically.
        def sort_key(item):
            key, info = item
            due = info["due"]
            remaining = info["seconds_until_due"]
            return (0 if due else 1, remaining if remaining is not None else 0, key)

        for key, info in sorted(stats.items(), key=sort_key):
            if not info["introduced"]:
                continue
            existing_s, add_s = key.split("+")
            pattern = classifier.classify(int(existing_s), int(add_s))
            status = "DUE" if info["due"] else f"{int(info['seconds_until_due'] or 0)}s"
            color = (0.9, 0.5, 0.4, 1) if info["due"] else (0.6, 0.6, 0.7, 1)
            for text in (key, pattern.label, str(info["repetitions"]), status):
                grid.add_widget(Label(text=text, color=color, size_hint_y=None, height=26))


class FlashAnzanApp(App):
    def build(self):
        storage_path = os.path.join(self.user_data_dir, "srs_state.json")
        self.classifier = AbacusPatternClassifier()
        self.scheduler = SpacedRepetitionScheduler(
            JSONFileStorage(storage_path),
            SchedulerConfig(),
        )
        self.session = FlashAnzanSession(
            self.classifier,
            self.scheduler,
            NumberSequenceGenerator(self.classifier, self.scheduler),
        )

        self.current_round = None
        self.current_settings = SessionSettings()
        self.last_result_correct = None
        self.last_user_answer = None

        sm = ScreenManager(transition=SlideTransition())
        sm.add_widget(SettingsScreen(name="settings"))
        sm.add_widget(FlashScreen(name="flash"))
        sm.add_widget(AnswerScreen(name="answer"))
        sm.add_widget(ResultScreen(name="result"))
        sm.add_widget(StatsScreen(name="stats"))
        self.sm = sm
        return sm

    # -- flow control, called from screens -------------------------------------------------------

    def begin_round(self, settings: SessionSettings):
        self.current_settings = settings
        self.current_round = self.session.start_round(settings)
        self.sm.current = "flash"

    def go_to_answer(self):
        self.sm.current = "answer"

    def stop_round(self):
        """User hit Stop on the flash screen: bail out without grading,
        go back to settings, and offer a quick way to report a bug."""
        self.sm.current = "settings"
        Factory.BugReportPopup().open()

    def submit_bug_report(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        report_path = os.path.join(self.user_data_dir, "bug_reports.log")
        try:
            with open(report_path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
        except OSError:
            # Never let a failed report crash the app.
            pass

    def grade_answer(self, value: int):
        self.last_user_answer = value
        self.last_result_correct = self.session.submit_answer(value)
        self.sm.current = "result"

    # Names for each digit column, by position (0 = rightmost/units).
    # Anything beyond this list just gets labelled "10^n place".
    PLACE_NAMES = ["1s", "10s", "100s", "1000s", "10000s"]

    def _place_name(self, position: int) -> str:
        if position < len(self.PLACE_NAMES):
            return self.PLACE_NAMES[position]
        return f"10^{position}s"

    def summarize_round_patterns(self, round_data) -> str:
        """Report every column-addition fact practiced this round, grouped
        by *which digit place it happened on* -- e.g. for 53 + 26 that's
        the 1s place (3+6) reported separately from the 10s place (5+2) --
        instead of collapsing every column into a single combined count."""
        steps = getattr(round_data, "steps", None)
        if not steps:
            return ""

        by_position = {}
        for step in steps:
            by_position.setdefault(step.position, []).append(step)

        lines = []
        for position in sorted(by_position):
            facts = []
            for step in by_position[position]:
                pattern = self.classifier.classify(step.existing, step.add)
                facts.append(f"{step.existing}+{step.add} ({pattern.label})")
            lines.append(f"{self._place_name(position)} place -> " + ", ".join(facts))
        return "\n".join(lines)


if __name__ == "__main__":
    FlashAnzanApp().run()
