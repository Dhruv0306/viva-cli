"""Tests for RichSessionUI. `read_answer` previously used rich.Live to
redraw a countdown in place, which could corrupt already-echoed answer
text when the person's own typing spanned multiple lines (a real bug
found during real-repo testing -- see
docs/system-design/11-phase-6-session-loop-design.md §11.9). These tests
exercise the fixed, append-only-prints version with a fake stdin/console
(io.StringIO) instead of a real TTY.
"""
from __future__ import annotations

import io

from rich.console import Console

from viva.session_ui import RichSessionUI, SessionSummary
from viva.timer import AnswerTimer


def _ui_with_captured_output() -> tuple[RichSessionUI, io.StringIO]:
    buffer = io.StringIO()
    console = Console(file=buffer, no_color=True, width=100)
    return RichSessionUI(console=console), buffer


def test_read_answer_returns_full_stdin_content(monkeypatch):
    ui, _buffer = _ui_with_captured_output()
    monkeypatch.setattr("sys.stdin", io.StringIO("This is my answer."))
    timer = AnswerTimer(60)
    timer.start()

    answer = ui.read_answer(timer)

    assert answer == "This is my answer."


def test_read_answer_strips_surrounding_whitespace(monkeypatch):
    ui, _buffer = _ui_with_captured_output()
    monkeypatch.setattr("sys.stdin", io.StringIO("\n  padded answer  \n"))
    timer = AnswerTimer(60)
    timer.start()

    answer = ui.read_answer(timer)

    assert answer == "padded answer"


def test_read_answer_prints_initial_time_remaining(monkeypatch):
    ui, buffer = _ui_with_captured_output()
    monkeypatch.setattr("sys.stdin", io.StringIO("answer"))
    timer = AnswerTimer(120)
    timer.start()

    ui.read_answer(timer)

    assert "remaining" in buffer.getvalue()


def test_read_answer_never_emits_cursor_repositioning_codes(monkeypatch):
    """The new implementation only ever calls console.print() -- appending
    fresh lines -- never redrawing in place, so it structurally cannot
    reposition the cursor and clobber already-printed answer text the way
    the old rich.Live-based version could (docs/system-design/
    11-phase-6-session-loop-design.md §11.9).

    Note on what this test does and doesn't prove: this is a fast,
    single-chunk fake stdin, so it doesn't reproduce the actual bug
    (that required several seconds of real typing plus multiple Live
    redraws racing against terminal echo -- not practical to reproduce
    deterministically without a real pty). Verified instead by checking
    the old Live-based code against the same assertions here: it also
    passed, because Live(transient=True) never got the chance to redraw
    more than once before this fast fake stdin returned. This test is a
    structural guarantee for the new code, not a reproduction of the bug.
    """
    ui, buffer = _ui_with_captured_output()
    monkeypatch.setattr("sys.stdin", io.StringIO("answer"))
    timer = AnswerTimer(120)
    timer.start()

    ui.read_answer(timer)

    output = buffer.getvalue()
    # Cursor-up / erase-line / hide-cursor sequences Live's in-place
    # redraw relies on.
    for seq in ("\x1b[1A", "\x1b[2K", "\x1b[?25l"):
        assert seq not in output


def test_time_expired_prints_notice():
    ui, buffer = _ui_with_captured_output()

    ui.time_expired()

    assert "up" in buffer.getvalue().lower()


def test_session_started_prints_session_id():
    ui, buffer = _ui_with_captured_output()

    ui.session_started("abc123def456")

    assert "abc123def456" in buffer.getvalue()


def test_session_complete_prints_summary():
    ui, buffer = _ui_with_captured_output()
    summary = SessionSummary(
        session_id="sess1", status="COMPLETE",
        questions_asked=5, questions_answered=4, questions_skipped=1,
    )

    ui.session_complete(summary)

    output = buffer.getvalue()
    assert "COMPLETE" in output
    assert "5" in output and "4" in output


def test_error_prints_message():
    ui, buffer = _ui_with_captured_output()

    ui.error("something went wrong")

    assert "something went wrong" in buffer.getvalue()
