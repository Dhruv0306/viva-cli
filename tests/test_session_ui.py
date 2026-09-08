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
import re
import threading
import time

import pytest
from prompt_toolkit.application import Application as _RealApplication
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

import viva.session_ui as session_ui_module
from viva.session_ui import RichSessionUI, SessionSummary, _submit_key_bindings
from viva.timer import AnswerTimer
from viva.voice_io import VoiceIOError


@pytest.fixture(autouse=True)
def _fake_prompt_toolkit_application_for_voice_status_bar(monkeypatch):
    """Every `RichSessionUI.read_answer()` call in voice mode constructs
    a `prompt_toolkit` `Application` for the live recording status bar
    (`_run_recording_status_bar`) -- patch it globally for this whole
    test module so none of the many voice-mode tests that drive
    `read_answer()` end-to-end need a real TTY. Only `Application` is
    patched here, not `PromptSession` (the typed-input path) -- tests of
    that path fake it separately via `_read_answer_with_keystrokes`,
    since those need per-test scripted keystrokes rather than one shared
    empty input."""
    original_application = session_ui_module.Application
    with create_pipe_input() as pipe_input:
        def _fake_application(*args, **kwargs):
            return original_application(*args, input=pipe_input, output=DummyOutput(), **kwargs)

        monkeypatch.setattr(session_ui_module, "Application", _fake_application)
        yield


def _ui_with_captured_rich_output() -> tuple[RichSessionUI, io.StringIO]:
    buffer = io.StringIO()
    console = Console(file=buffer, no_color=True, width=100)
    return RichSessionUI(console=console), buffer


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)


def _run_recording_status_bar_capturing_output(
    ui: RichSessionUI, timer: AnswerTimer, thread: threading.Thread
) -> str:
    """Runs `RichSessionUI._run_recording_status_bar` against a real
    `prompt_toolkit.output.vt100.Vt100_Output` writing into an in-memory
    buffer, rather than `DummyOutput` (which discards everything) --
    needed to actually assert on what the live status bar renders, the
    same way `_ui_with_captured_rich_output`'s `Console(file=buffer)`
    captures Rich's side of the output. `enable_cpr=False` avoids
    blocking on a cursor-position-report response that a fake pipe input
    never sends."""
    original_application = session_ui_module.Application
    buffer = io.StringIO()
    output = Vt100_Output(buffer, lambda: Size(rows=24, columns=100), enable_cpr=False)

    with create_pipe_input() as pipe_input:
        def _fake_application(*args, **kwargs):
            return _RealApplication(*args, input=pipe_input, output=output, **kwargs)

        session_ui_module.Application = _fake_application
        try:
            ui._run_recording_status_bar(timer, thread)
        finally:
            session_ui_module.Application = original_application

    return _strip_ansi(buffer.getvalue())


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


# --- Voice mode (Phase 11, docs/system-design/16-phase-11-voice-io-design.md)


class _FakeVoiceIO:
    """Scripted stand-in for `voice_io.VoiceIO` -- no real audio
    hardware or models, mirroring FakeSessionUI's role for `SessionUI`
    in test_orchestrator.py."""

    def __init__(self, spoken_text: str | None = None, transcribed_text: str | None = None) -> None:
        self.spoken: list[str] = []
        self.speak_error: VoiceIOError | None = None
        self.record_error: VoiceIOError | None = None
        self.transcribe_error: VoiceIOError | None = None
        self.record_return: bytes | None = b"fake-audio"
        self.transcribed_text = transcribed_text
        self.record_calls: list[tuple[float, float]] = []

    def speak(self, text: str) -> None:
        if self.speak_error is not None:
            raise self.speak_error
        self.spoken.append(text)

    def record(self, max_seconds: float, silence_timeout: float) -> bytes | None:
        self.record_calls.append((max_seconds, silence_timeout))
        if self.record_error is not None:
            raise self.record_error
        return self.record_return

    def transcribe(self, audio: bytes) -> str | None:
        if self.transcribe_error is not None:
            raise self.transcribe_error
        return self.transcribed_text


def _voice_ui_with_captured_rich_output(voice: _FakeVoiceIO) -> tuple[RichSessionUI, io.StringIO]:
    buffer = io.StringIO()
    console = Console(file=buffer, no_color=True, width=100)
    return RichSessionUI(console=console, voice=voice), buffer


def test_ask_question_speaks_the_question_when_voice_enabled():
    voice = _FakeVoiceIO()
    ui, buffer = _voice_ui_with_captured_rich_output(voice)

    ui.ask_question("Why does X do Y?", "architecture", 1)

    assert voice.spoken == ["Why does X do Y?"]
    # The question is still printed too -- voice augments, not replaces.
    assert "Why does X do Y?" in buffer.getvalue()


def test_ask_question_without_voice_never_calls_speak():
    ui, _buffer = _ui_with_captured_rich_output()
    # Sanity check that plain RichSessionUI() (no voice arg) truly has no
    # voice attached -- covered implicitly by every other text-mode test
    # in this file never raising, but asserted explicitly here.
    assert ui._voice is None


def test_ask_question_playback_failure_falls_back_to_silent_question_display():
    voice = _FakeVoiceIO()
    voice.speak_error = VoiceIOError("speaker unplugged")
    ui, buffer = _voice_ui_with_captured_rich_output(voice)

    ui.ask_question("Why does X do Y?", "architecture", 1)

    # Doesn't raise, and the question is still visible.
    assert "Why does X do Y?" in buffer.getvalue()
    assert "speaker unplugged" in buffer.getvalue()


def test_read_answer_transcribes_a_spoken_answer():
    voice = _FakeVoiceIO(transcribed_text="the answer is a dataclass")
    ui, buffer = _voice_ui_with_captured_rich_output(voice)
    timer = AnswerTimer(60)
    timer.start()

    answer = ui.read_answer(timer)

    assert answer == "the answer is a dataclass"
    assert "transcribed" in buffer.getvalue().lower()


def test_read_answer_falls_back_to_text_on_silence(monkeypatch):
    voice = _FakeVoiceIO()
    voice.record_return = None  # nothing crossed the amplitude threshold
    ui, buffer = _voice_ui_with_captured_rich_output(voice)
    timer = AnswerTimer(60)
    timer.start()

    answer = _read_answer_with_keystrokes(ui, timer, "typed instead\x1b\r")

    assert answer == "typed instead"
    assert "no speech detected" in buffer.getvalue().lower()
    assert "falling back to typed input" in buffer.getvalue().lower()


def test_read_answer_falls_back_to_text_on_empty_transcription():
    voice = _FakeVoiceIO(transcribed_text=None)  # e.g. pure background noise
    ui, buffer = _voice_ui_with_captured_rich_output(voice)
    timer = AnswerTimer(60)
    timer.start()

    answer = _read_answer_with_keystrokes(ui, timer, "typed instead\x1b\r")

    assert answer == "typed instead"
    assert "could not transcribe" in buffer.getvalue().lower()


def test_read_answer_falls_back_to_text_on_recording_hardware_failure():
    voice = _FakeVoiceIO()
    voice.record_error = VoiceIOError("microphone unplugged")
    ui, buffer = _voice_ui_with_captured_rich_output(voice)
    timer = AnswerTimer(60)
    timer.start()

    answer = _read_answer_with_keystrokes(ui, timer, "typed instead\x1b\r")

    assert answer == "typed instead"
    assert "microphone unplugged" in buffer.getvalue()


def test_read_answer_falls_back_to_text_on_transcription_failure():
    voice = _FakeVoiceIO()
    voice.transcribe_error = VoiceIOError("model crashed")
    ui, buffer = _voice_ui_with_captured_rich_output(voice)
    timer = AnswerTimer(60)
    timer.start()

    answer = _read_answer_with_keystrokes(ui, timer, "typed instead\x1b\r")

    assert answer == "typed instead"
    assert "model crashed" in buffer.getvalue()


def test_read_answer_recording_time_is_not_excluded_from_the_timer():
    """§16.4: recording counts as answering time, unlike transcription
    compute -- so record() must be called outside timer.excluding()."""
    voice = _FakeVoiceIO(transcribed_text="an answer")
    ui, _buffer = _voice_ui_with_captured_rich_output(voice)
    timer = AnswerTimer(60)
    timer.start()

    excluding_active_during_record = []
    original_record = voice.record

    def _spying_record(max_seconds, silence_timeout):
        excluding_active_during_record.append(timer._excluded_seconds)
        return original_record(max_seconds, silence_timeout)

    voice.record = _spying_record
    ui.read_answer(timer)

    # timer.excluding() hasn't run yet at the point record() is called --
    # transcribe() (called after) is what accumulates _excluded_seconds.
    assert excluding_active_during_record == [0.0]


def test_read_answer_caps_recording_at_remaining_time():
    voice = _FakeVoiceIO(transcribed_text="an answer")
    ui, _buffer = _voice_ui_with_captured_rich_output(voice)
    timer = AnswerTimer(5)  # 5s total budget, well under the 120s default cap
    timer.start()

    ui.read_answer(timer)

    (max_seconds_used, _silence_timeout), = voice.record_calls
    assert max_seconds_used <= 5.0


def test_read_answer_shows_a_countdown_while_recording():
    # Regression test for a real-world gap (docs/system-design/
    # 16-phase-11-voice-io-design.md §16.9): the typed-input path has
    # always shown a live remaining-time toolbar, but the original
    # voice-mode recording path printed nothing at all while blocked
    # waiting on record() -- someone using voice mode had no visibility
    # into the clock running down at all.
    ui, _buffer = _ui_with_captured_rich_output()
    timer = AnswerTimer(60)
    timer.start()

    # A thread that's still "alive" for one status-bar redraw tick
    # before finishing, so _run_recording_status_bar has something to
    # render before it exits.
    still_running = threading.Event()

    def _briefly_alive():
        still_running.wait(timeout=2)

    thread = threading.Thread(target=_briefly_alive)
    thread.start()

    def _release_after_a_tick():
        time.sleep(0.7)  # past the 0.5s refresh_interval
        still_running.set()

    releaser = threading.Thread(target=_release_after_a_tick)
    releaser.start()

    rendered = _run_recording_status_bar_capturing_output(ui, timer, thread)
    thread.join()
    releaser.join()

    assert "Recording" in rendered
    assert "remaining" in rendered.lower()


def test_read_answer_status_bar_returns_promptly_once_recording_finishes():
    """The status bar must not outlive the recording thread -- its exit
    check runs from inside the refresh_interval callback (§ design doc
    16.9), not from a separate poll loop, so this also covers that the
    Application actually exits rather than hanging."""
    ui, _buffer = _ui_with_captured_rich_output()
    timer = AnswerTimer(60)
    timer.start()

    already_finished_thread = threading.Thread(target=lambda: None)
    already_finished_thread.start()
    already_finished_thread.join()

    start = time.monotonic()
    _run_recording_status_bar_capturing_output(ui, timer, already_finished_thread)
    elapsed = time.monotonic() - start

    # Should return on (at most) the first refresh tick, not linger.
    assert elapsed < 1.5
