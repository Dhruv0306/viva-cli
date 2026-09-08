"""`SessionUI`: the Orchestrator's interface to whatever's presenting the
live session to a person (docs/plan.md Phase 6, FR17-FR19).

Mirrors the `LLMClient`/`EmbeddingClient` seam pattern (NFR5, "thin
interfaces"): `orchestrator.py` only ever talks to this ABC, never to
`rich`/`prompt_toolkit` directly, so the session loop can be tested with
a scripted fake UI instead of a real terminal (CONTRIBUTING.md: "the
test suite must run without" -- here, without a real TTY).

`RichSessionUI` is the one real implementation. Answer input uses
`prompt_toolkit` (Alt+Enter to submit, plain Enter for a new line, a
live-refreshing bottom toolbar showing time remaining) rather than
`rich`'s `Live` or a raw blocking `sys.stdin.read()` -- both prior
approaches are documented in
docs/system-design/11-phase-6-session-loop-design.md §11.9, including a
real corruption bug the `Live`-based version had. `prompt_toolkit` owns
its own render region coherently (no cursor-desync risk) and has native
key-binding support, which a raw EOF-terminated read never could.

**Known limitation** (unchanged from earlier versions -- see the design
doc's "Known limitations"): if the timer expires while the person is
still composing an answer, the toolbar switches to a "time's up"
message, but `read_answer()` still blocks until they press Alt+Enter --
nothing forcibly cuts the input short. This doesn't affect FR17's
actual guarantee (LLM/eval latency exclusion from the clock), only how
promptly typing is cut off once time runs out.
"""
from __future__ import annotations

import abc
import threading
from dataclasses import dataclass

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from viva.timer import AnswerTimer
from viva.voice_io import VoiceIO, VoiceIOError


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    status: str
    questions_asked: int
    questions_answered: int
    questions_skipped: int


class SessionUI(abc.ABC):
    @abc.abstractmethod
    def session_started(self, session_id: str) -> None: ...

    @abc.abstractmethod
    def stage_started(self, stage: str) -> None: ...

    @abc.abstractmethod
    def stage_completed(self, stage: str, detail: str) -> None: ...

    @abc.abstractmethod
    def ask_question(self, question_text: str, category: str, question_number: int) -> None: ...

    @abc.abstractmethod
    def read_answer(self, timer: AnswerTimer) -> str:
        """Block until the person submits an answer (or the process
        receives EOF/interrupt), returning what they typed."""
        ...

    @abc.abstractmethod
    def time_expired(self) -> None: ...

    @abc.abstractmethod
    def session_complete(self, summary: SessionSummary) -> None: ...

    @abc.abstractmethod
    def error(self, message: str) -> None: ...


def _submit_key_bindings() -> KeyBindings:
    """Alt+Enter submits; plain Enter inserts a newline (multiline
    composition). Terminals conventionally report Alt+<key> as an ESC
    byte followed by <key> -- `('escape', 'enter')` is prompt_toolkit's
    documented idiom for binding Alt+Enter, and on Windows
    prompt_toolkit's own console backend detects the real Alt modifier
    at the OS level rather than relying on that ESC-prefix convention,
    so this works consistently cross-platform.
    """
    bindings = KeyBindings()

    @bindings.add("escape", "enter")
    def _submit(event) -> None:
        event.current_buffer.validate_and_handle()

    return bindings


def _recording_status_key_bindings() -> KeyBindings:
    """Ctrl+C interrupts a recording in progress, matching
    `prompt_toolkit`'s own default `prompt()` behavior -- the recording
    status bar has nothing to submit (nothing is being typed), so this
    is the only binding it needs."""
    bindings = KeyBindings()

    @bindings.add("c-c")
    def _interrupt(event) -> None:
        event.app.exit(exception=KeyboardInterrupt)

    return bindings


class RichSessionUI(SessionUI):
    def __init__(
        self,
        console: Console | None = None,
        voice: VoiceIO | None = None,
        voice_max_answer_seconds: float = 120.0,
        voice_silence_timeout_seconds: float = 2.5,
    ) -> None:
        """`voice`, if given, enables Phase 11 voice mode (docs/system-
        design/16-phase-11-voice-io-design.md): `ask_question()` speaks
        the question aloud (in addition to printing it, not instead of),
        and `read_answer()` records and transcribes a spoken answer
        instead of prompting for typed input. `None` (the default) keeps
        text-only behavior exactly as before this phase.
        """
        self._console = console or Console()
        self._voice = voice
        self._voice_max_answer_seconds = voice_max_answer_seconds
        self._voice_silence_timeout_seconds = voice_silence_timeout_seconds

    def session_started(self, session_id: str) -> None:
        self._console.print(f"[bold]Session started:[/bold] {session_id}")
        self._console.print(
            "[dim]Save this ID -- it's not shown again except via `viva list`.[/dim]"
        )

    def stage_started(self, stage: str) -> None:
        self._console.print(f"[cyan]{stage}...[/cyan]")

    def stage_completed(self, stage: str, detail: str) -> None:
        self._console.print(f"[green]{stage} complete[/green] -- {detail}")

    def ask_question(self, question_text: str, category: str, question_number: int) -> None:
        self._console.print()
        self._console.print(
            Panel(question_text, title=f"Question {question_number} [{category}]")
        )
        if self._voice is not None:
            try:
                self._voice.speak(question_text)
            except VoiceIOError as exc:
                # Playback failure doesn't disable voice mode for the
                # rest of the session (§16.6) -- the question is already
                # on screen either way, so this is a degraded-but-fine
                # continuation, not a fallback trigger.
                self._console.print(f"[yellow]Couldn't speak the question aloud: {escape(str(exc))}[/yellow]")
            self._console.print(
                "[dim]Recording will start automatically -- speak your answer, "
                "or stay silent to type instead.[/dim]"
            )
        else:
            self._console.print(
                "[dim]Type your answer (Enter for a new line). Press Alt+Enter to submit.[/dim]"
            )

    def _run_recording_status_bar(self, timer: AnswerTimer, thread: threading.Thread) -> None:
        """A live, in-place-updating status line while `thread` (running
        `VoiceIO.record()`) is alive -- fixing a real-world gap
        (docs/system-design/16-phase-11-voice-io-design.md \u00a716.9):
        typed input has always had a live countdown toolbar
        (`_read_answer_by_text`'s `bottom_toolbar`), but the first
        version of this method was sparse, scrolling `console.print()`
        checkpoints instead of a genuinely live, fixed-position bar --
        reported directly against a screenshot of the typed-input
        toolbar asking why voice mode didn't look the same.

        Uses a minimal `prompt_toolkit.Application` rather than
        `rich.Live`, for the same reason `_read_answer_by_text` does
        (\u00a711.9's documented corruption bug) -- `prompt_toolkit` owns its
        own render region coherently, and this gives the identical
        look-and-feel to the typed-input toolbar rather than a
        different, bolted-on mechanism for voice mode specifically.

        The liveness/exit check runs inside the status text getter
        itself, which `refresh_interval` invokes from the Application's
        own event loop -- so exiting the Application when recording
        finishes never has to signal a running event loop from another
        thread; it happens naturally on the next scheduled redraw tick.
        """
        status_bar_state = {"exited": False}

        def _status_text() -> HTML:
            if not thread.is_alive():
                if not status_bar_state["exited"]:
                    status_bar_state["exited"] = True
                    get_app().exit()
                return HTML("")
            return HTML(
                '<style fg="ansicyan">\U0001f3a4 Recording...</style>  '
                f'<style fg="ansiyellow">\u23f1  {timer.format_remaining()} remaining</style>'
            )

        app: Application = Application(
            layout=Layout(Window(content=FormattedTextControl(_status_text), height=1)),
            key_bindings=_recording_status_key_bindings(),
            refresh_interval=0.5,
            full_screen=False,
        )
        try:
            app.run()
        except KeyboardInterrupt:
            pass

    def _read_answer_by_voice(self, timer: AnswerTimer) -> str | None:
        """Records and transcribes a spoken answer. Returns None (rather
        than raising) on any of the three fallback triggers design doc
        §16.6 lists -- silence, empty transcription, or a hardware
        failure -- so `read_answer()` can fall through to the typed-input
        path for this one question without disabling voice mode for the
        rest of the session.
        """
        assert self._voice is not None
        max_seconds = min(self._voice_max_answer_seconds, timer.remaining())
        if max_seconds <= 0:
            return None

        result: dict = {}

        def _do_record() -> None:
            try:
                # Not wrapped in timer.excluding() -- recording is the
                # person's answering time, the same as typing would be
                # (§16.4's correction to the pre-implementation plan).
                result["audio"] = self._voice.record(max_seconds, self._voice_silence_timeout_seconds)
            except VoiceIOError as exc:
                result["error"] = exc

        record_thread = threading.Thread(target=_do_record, daemon=True)
        record_thread.start()
        self._run_recording_status_bar(timer, record_thread)
        record_thread.join()

        if "error" in result:
            self._console.print(f"[red]Recording failed: {escape(str(result['error']))}[/red]")
            return None
        audio = result.get("audio")

        if audio is None:
            self._console.print("[yellow]No speech detected.[/yellow]")
            return None

        with timer.excluding():
            try:
                answer_text = self._voice.transcribe(audio)
            except VoiceIOError as exc:
                self._console.print(f"[red]Transcription failed: {escape(str(exc))}[/red]")
                return None

        if not answer_text:
            self._console.print("[yellow]Could not transcribe any speech.[/yellow]")
            return None

        word_count = len(answer_text.split())
        plural = "" if word_count == 1 else "s"
        self._console.print(
            f"[green]\u2713 Answer transcribed ({word_count} word{plural}):[/green] {answer_text}"
        )
        return answer_text

    def read_answer(self, timer: AnswerTimer) -> str:
        if self._voice is not None:
            answer = self._read_answer_by_voice(timer)
            if answer is not None:
                return answer
            self._console.print("[yellow]Falling back to typed input for this question.[/yellow]")
        return self._read_answer_by_text(timer)

    def _read_answer_by_text(self, timer: AnswerTimer) -> str:
        """`prompt_toolkit` owns the whole input region -- the bottom
        toolbar and the multi-line buffer are rendered coherently by the
        same event loop, so (unlike the two prior implementations) there
        is no risk of a countdown redraw landing on top of and corrupting
        already-typed text. `refresh_interval` gives a genuinely
        continuously-updating countdown, not periodic snapshots.
        """
        def _toolbar() -> HTML:
            if timer.expired():
                return HTML(
                    '<style fg="ansired">Time is up -- press Alt+Enter to submit '
                    "what you have.</style>"
                )
            return HTML(
                f'<style fg="ansiyellow">\u23f1  {timer.format_remaining()} remaining</style>'
                '  <style fg="ansigray">(Alt+Enter to submit)</style>'
            )

        session: PromptSession[str] = PromptSession(key_bindings=_submit_key_bindings())
        try:
            answer = session.prompt(
                "> ", multiline=True, bottom_toolbar=_toolbar, refresh_interval=0.5,
            )
        except (EOFError, KeyboardInterrupt):
            answer = ""

        answer = answer.strip()
        if answer:
            word_count = len(answer.split())
            plural = "" if word_count == 1 else "s"
            self._console.print(f"[green]\u2713 Answer recorded ({word_count} word{plural}).[/green]")
        else:
            self._console.print("[yellow]No answer recorded (empty response).[/yellow]")
        return answer

    def time_expired(self) -> None:
        self._console.print("[yellow]Time's up.[/yellow]")

    def session_complete(self, summary: SessionSummary) -> None:
        self._console.print()
        self._console.print(
            Panel(
                f"Status: {summary.status}\n"
                f"Questions asked: {summary.questions_asked}\n"
                f"Questions answered: {summary.questions_answered}\n"
                f"Questions skipped: {summary.questions_skipped}",
                title="Session summary",
            )
        )
        self._console.print(
            f"[dim]Resume later with `viva resume {summary.session_id}` if not complete, "
            f"or view results with `viva report {summary.session_id}` once evaluation exists.[/dim]"
        )

    def error(self, message: str) -> None:
        self._console.print(f"[red]{message}[/red]")
