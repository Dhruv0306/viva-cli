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
from dataclasses import dataclass

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.panel import Panel

from viva.timer import AnswerTimer


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


class RichSessionUI(SessionUI):
    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

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
        self._console.print(
            "[dim]Type your answer (Enter for a new line). Press Alt+Enter to submit.[/dim]"
        )

    def read_answer(self, timer: AnswerTimer) -> str:
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
