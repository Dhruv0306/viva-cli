"""Tests for RichSessionUI. `read_answer` uses prompt_toolkit for
multi-line, Alt+Enter-submitted input with a live-refreshing bottom
toolbar (docs/system-design/11-phase-6-session-loop-design.md §11.9,
replacing two earlier implementations -- a raw sys.stdin.read() and a
rich.Live-based countdown that could corrupt echoed text).

prompt_toolkit ships first-class testing support for exactly this
scenario: `create_pipe_input()` simulates real keystrokes (including
Alt+Enter, sent as the terminal-convention ESC+Enter byte sequence)
without needing a real TTY, and `DummyOutput` discards rendered output
so tests don't need a real terminal to write to either.
"""
from __future__ import annotations

import io

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

import viva.session_ui as session_ui_module
from viva.session_ui import RichSessionUI, SessionSummary, _submit_key_bindings
from viva.timer import AnswerTimer


def _ui_with_captured_rich_output() -> tuple[RichSessionUI, io.StringIO]:
    buffer = io.StringIO()
    console = Console(file=buffer, no_color=True, width=100)
    return RichSessionUI(console=console), buffer


def _read_answer_with_keystrokes(ui: RichSessionUI, timer: AnswerTimer, keystrokes: str) -> str:
    """Feeds raw keystrokes through a fake terminal input, monkeypatching
    the module-level PromptSession so RichSessionUI uses the pipe input
    instead of a real TTY."""
    original_prompt_session = session_ui_module.PromptSession

    with create_pipe_input() as pipe_input:
        pipe_input.send_text(keystrokes)

        def _fake_prompt_session(*args, **kwargs):
            return original_prompt_session(*args, input=pipe_input, output=DummyOutput(), **kwargs)

        session_ui_module.PromptSession = _fake_prompt_session
        try:
            return ui.read_answer(timer)
        finally:
            session_ui_module.PromptSession = original_prompt_session


def test_alt_enter_submits_single_line_answer():
    ui, _buffer = _ui_with_captured_rich_output()
    timer = AnswerTimer(60)
    timer.start()

    answer = _read_answer_with_keystrokes(ui, timer, "hello there\x1b\r")

    assert answer == "hello there"


def test_plain_enter_inserts_newline_not_submit():
    """Plain Enter must compose a multi-line answer, not submit --
    Alt+Enter is the only submit trigger."""
    ui, _buffer = _ui_with_captured_rich_output()
    timer = AnswerTimer(60)
    timer.start()

    answer = _read_answer_with_keystrokes(ui, timer, "first line\rsecond line\x1b\r")

    assert answer == "first line\nsecond line"


def test_read_answer_strips_surrounding_whitespace():
    ui, _buffer = _ui_with_captured_rich_output()
    timer = AnswerTimer(60)
    timer.start()

    answer = _read_answer_with_keystrokes(ui, timer, "  padded  \x1b\r")

    assert answer == "padded"


def test_read_answer_prints_confirmation_after_submission():
    """Addresses the 'ghost talk' feedback gap -- immediate, visible
    confirmation that the answer was recorded, right after submission."""
    ui, buffer = _ui_with_captured_rich_output()
    timer = AnswerTimer(60)
    timer.start()

    _read_answer_with_keystrokes(ui, timer, "some words here\x1b\r")

    output = buffer.getvalue()
    assert "recorded" in output.lower()
    assert "3 word" in output  # "some words here" -- word count shown


def test_read_answer_reports_empty_answer_distinctly():
    ui, buffer = _ui_with_captured_rich_output()
    timer = AnswerTimer(60)
    timer.start()

    answer = _read_answer_with_keystrokes(ui, timer, "\x1b\r")

    assert answer == ""
    assert "no answer" in buffer.getvalue().lower()


def test_eof_returns_empty_string_not_an_exception():
    ui, _buffer = _ui_with_captured_rich_output()
    timer = AnswerTimer(60)
    timer.start()

    # Ctrl-D on an empty buffer raises EOFError inside prompt_toolkit;
    # read_answer must handle it gracefully rather than propagating.
    answer = _read_answer_with_keystrokes(ui, timer, "\x04")

    assert answer == ""


def test_submit_key_bindings_only_bind_escape_enter():
    bindings = _submit_key_bindings()
    bound_sequences = [tuple(k.value for k in b.keys) for b in bindings.bindings]
    # Enter is internally Keys.ControlM ('c-m') in prompt_toolkit.
    assert any("escape" in seq and "c-m" in seq for seq in bound_sequences)


def test_time_expired_prints_notice():
    ui, buffer = _ui_with_captured_rich_output()

    ui.time_expired()

    assert "up" in buffer.getvalue().lower()


def test_session_started_prints_session_id():
    ui, buffer = _ui_with_captured_rich_output()

    ui.session_started("abc123def456")

    assert "abc123def456" in buffer.getvalue()


def test_ask_question_mentions_alt_enter():
    ui, buffer = _ui_with_captured_rich_output()

    ui.ask_question("Why does X do Y?", "architecture", 1)

    assert "Alt" in buffer.getvalue()


def test_session_complete_prints_summary():
    ui, buffer = _ui_with_captured_rich_output()
    summary = SessionSummary(
        session_id="sess1", status="COMPLETE",
        questions_asked=5, questions_answered=4, questions_skipped=1,
    )

    ui.session_complete(summary)

    output = buffer.getvalue()
    assert "COMPLETE" in output
    assert "5" in output and "4" in output


def test_error_prints_message():
    ui, buffer = _ui_with_captured_rich_output()

    ui.error("something went wrong")

    assert "something went wrong" in buffer.getvalue()
