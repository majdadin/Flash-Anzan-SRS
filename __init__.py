"""
Core engine for the Flash Anzan spaced-repetition trainer.

This package has no dependency on Kivy (or any UI toolkit) on purpose —
`patterns.py`, `scheduler.py`, `generator.py` and `session.py` are plain
Python so they can be unit-tested and reused from a CLI, a different UI,
or a future web/API front-end. `main.py` (outside this package) is the
only file that touches Kivy.
"""
