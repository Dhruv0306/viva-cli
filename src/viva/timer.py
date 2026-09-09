"""The user-facing answer clock (FR17, docs/design.md §7).

The session clock must reflect answering time only -- LLM generation and
evaluation latency must never consume the user's allotted time. `AnswerTimer`
uses `time.monotonic()` (immune to wall-clock adjustments) and an explicit
`excluding()` context manager that any code performing an LLM call must wrap
itself in, so exclusion is opt-in and visible at each call site rather than
inferred.

Thread-safe as of Phase 12 (docs/system-design/
17-phase-12-web-voice-io-design.md §17.5): every prior caller of
`excluding()` ran on the same single thread as everything else touching a
given `AnswerTimer`. The web voice layer's `transcribe()` call is the first
caller from a *different* thread than the Orchestrator's own loop, running
concurrently with `WebSessionUI.snapshot()`'s own `remaining()` read from a
polling request. A `threading.Lock` around `_start`/`_excluded_seconds`
makes each read or mutation atomic; no behavior change for the CLI's
existing single-threaded usage.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator


class TimerNotStartedError(RuntimeError):
    pass


class AnswerTimer:
    def __init__(self, duration_seconds: float) -> None:
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        self.duration_seconds = duration_seconds
        self._start: float | None = None
        self._excluded_seconds: float = 0.0
        self._lock = threading.Lock()

    def start(self, initial_elapsed_seconds: float = 0.0) -> None:
        """`initial_elapsed_seconds` lets a resumed session (Phase 6,
        docs/system-design/11-phase-6-session-loop-design.md) restore
        answer time already spent in a prior process before it crashed or
        was interrupted, rather than granting the full duration again.
        """
        with self._lock:
            self._start = time.monotonic() - initial_elapsed_seconds
            self._excluded_seconds = 0.0

    @contextmanager
    def excluding(self) -> Iterator[None]:
        """Wrap any LLM call (generation or evaluation) in this block.

        Time spent inside is subtracted from `elapsed()`, so it never counts
        against the user's answering time.
        """
        exclusion_start = time.monotonic()
        try:
            yield
        finally:
            elapsed_in_block = time.monotonic() - exclusion_start
            with self._lock:
                self._excluded_seconds += elapsed_in_block

    def elapsed(self) -> float:
        """Answer time consumed so far, excluding any `excluding()` blocks."""
        with self._lock:
            start = self._start
            excluded = self._excluded_seconds
        if start is None:
            raise TimerNotStartedError("AnswerTimer.start() was not called")
        return (time.monotonic() - start) - excluded

    def remaining(self) -> float:
        return max(0.0, self.duration_seconds - self.elapsed())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def format_remaining(self) -> str:
        """MM:SS for live-countdown display (FR17: must be a live countdown)."""
        total_seconds = round(self.remaining())
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes:02d}:{seconds:02d}"
