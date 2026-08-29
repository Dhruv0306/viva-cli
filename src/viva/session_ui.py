"""`SessionUI`: the Orchestrator's interface to whatever's presenting the
live session to a person (docs/plan.md Phase 6, FR17-FR19).

Mirrors the `LLMClient`/`EmbeddingClient` seam pattern (NFR5, "thin
interfaces"): `orchestrator.py` only ever talks to this ABC, never to
`rich`/`sys.stdin` directly, so the session loop can be tested with a
scripted fake UI instead of a real terminal (CONTRIBUTING.md: "the test
suite must run without" -- here, without a real TTY).

`RichSessionUI` is the one real implementation, printing periodic
time-remaining status lines for FR17's live-countdown requirement (see
`read_answer`'s docstring for why these are append-only prints rather
than a single continuously-redrawn line -- a real corruption bug found
during real-repo testing, documented in
docs/system-design/11-phase-6-session-loop-design.md §11.9).

**Known limitation** (see the design doc's "Known limitations"):
`read_answer()`'s background reader thread blocks on `sys.stdin.read()`
until EOF (Ctrl-D). If the timer expires while the person is still
typing, status printing stops and a "time's up" notice is shown, but
the read itself can't be forcibly interrupted from another thread --
whatever they've typed is still captured once they press Ctrl-D. This
doesn't affect FR17's actual guarantee (LLM/eval latency exclusion from
the clock), only how promptly typing is cut off.
"""
from __future__ import annotations

import abc
import sys
import threading
from dataclasses import dataclass

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
        """Block until the person finishes answering (or the process
        receives EOF), returning what they typed."""
        ...

    @abc.abstractmethod
    def time_expired(self) -> None: ...

    @abc.abstractmethod
    def session_complete(self, summary: SessionSummary) -> None: ...

    @abc.abstractmethod
    def error(self, message: str) -> None: ...


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
            "[dim]Type your answer, then press Ctrl-D (Ctrl-Z then Enter on Windows) "
            "to submit.[/dim]"
        )

    def read_answer(self, timer: AnswerTimer) -> str:
        """Reads the answer on a background thread (blocking on
        `sys.stdin.read()` until EOF) while the main thread prints
        periodic time-remaining lines.

        This used to use `rich.live.Live` to redraw a single line in
        place. `Live` assumes it has exclusive control of the terminal
        region it's redrawing, which broke down the moment the person's
        own multi-line answer got echoed by the terminal in between
        redraws -- `Live` has no visibility into those extra lines, so
        its next redraw's cursor math would land in the wrong place.
        Found during real-repo testing: this didn't just look messy, it
        could land mid-line and interleave with already-echoed answer
        text (docs/system-design/11-phase-6-session-loop-design.md
        §11.9). Plain `console.print()` calls only ever append -- never
        reposition the cursor -- so they can't corrupt anything, at the
        cost of a true smoothly-updating single line: status now updates
        on whole-minute boundaries plus 30s/10s urgency checkpoints
        rather than continuously.
        """
        result: dict[str, str] = {}
        done = threading.Event()

        def _read_stdin() -> None:
            result["text"] = sys.stdin.read()
            done.set()

        reader = threading.Thread(target=_read_stdin, daemon=True)
        reader.start()

        self._print_remaining(timer)
        last_minute_shown = int(timer.remaining() // 60)
        warned_30s = False
        warned_10s = False

        while not done.is_set():
            if timer.expired():
                break
            done.wait(1.0)
            if done.is_set() or timer.expired():
                break
            remaining = timer.remaining()
            current_minute = int(remaining // 60)
            if current_minute < last_minute_shown:
                last_minute_shown = current_minute
                self._print_remaining(timer)
            elif remaining <= 30 and not warned_30s:
                warned_30s = True
                self._print_remaining(timer, style="yellow")
            elif remaining <= 10 and not warned_10s:
                warned_10s = True
                self._print_remaining(timer, style="red")

        if timer.expired() and not done.is_set():
            self.time_expired()

        reader.join(timeout=0.1)
        return result.get("text", "").strip()

    def _print_remaining(self, timer: AnswerTimer, style: str = "dim") -> None:
        self._console.print(f"[{style}]\u23f1  {timer.format_remaining()} remaining[/{style}]")

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
