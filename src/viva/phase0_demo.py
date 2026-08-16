"""Phase 0 walking skeleton (docs/plan.md "Phase 0 — Walking Skeleton").

Deliberately NOT the real Orchestrator from docs/design.md §1 -- there is no
session state machine, no persistence, no ingestion/indexing here. This is
the thinnest possible slice through the whole pipeline shape, built solely
to de-risk two assumptions before deeper component work begins:

  1. Can a local model reliably produce a schema-validated, grounded
     evaluation (not just schema-valid but hallucinated)?
  2. Does the answer clock actually exclude LLM latency in practice, not
     just on paper?

Exit criteria (docs/plan.md): one schema-validated evaluation produced by
the local model end-to-end, its groundedness manually reviewed (not just
JSON validity), and a timer that demonstrably excludes LLM latency.
"""
from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.panel import Panel

from viva.llm_client import LLMClient
from viva.schemas import EvaluationResult
from viva.timer import AnswerTimer

# A single hardcoded question, grounded in a real (small, inline) code
# snippet -- standing in for QuestionGen (Phase 5) and Indexer/RAG (Phase 4),
# neither of which exist yet. The snippet below is this repo's own
# AnswerTimer.excluding(), so the demo is self-grounding and needs no clone
# step (Ingest is Phase 2).
HARDCODED_QUESTION = (
    "Look at the `excluding()` context manager below. Why does it use "
    "`time.monotonic()` instead of `time.time()`, and what specifically "
    "gets excluded from the answer clock when a caller wraps an LLM call "
    "in it?"
)

GROUND_TRUTH_CONTEXT = '''\
File: src/viva/timer.py

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
            self._excluded_seconds += time.monotonic() - exclusion_start

    def elapsed(self) -> float:
        """Answer time consumed so far, excluding any `excluding()` blocks."""
        if self._start is None:
            raise TimerNotStartedError("AnswerTimer.start() was not called")
        return (time.monotonic() - self._start) - self._excluded_seconds
'''


@dataclass
class DemoReport:
    question: str
    answer: str
    evaluation: EvaluationResult
    answer_time_seconds: float
    llm_latency_seconds: float

    def to_markdown(self) -> str:
        return (
            "# Phase 0 Walking Skeleton — Bare-Bones Report\n\n"
            f"**Question:** {self.question}\n\n"
            f"**Your answer:** {self.answer}\n\n"
            f"**Classification:** {self.evaluation.classification}\n\n"
            f"**Verdict:** {self.evaluation.summary}\n\n"
            f"**Cited file:** {self.evaluation.cited_file or '(none)'}\n\n"
            f"**Needs review:** {self.evaluation.needs_review}\n\n"
            "---\n\n"
            f"- Answer time counted against the clock: "
            f"{self.answer_time_seconds:.2f}s\n"
            f"- LLM evaluation latency (excluded from the clock): "
            f"{self.llm_latency_seconds:.2f}s\n"
        )


def run_demo(llm_client: LLMClient, duration_seconds: float, console: Console) -> DemoReport:
    """Run the Phase 0 walking skeleton end-to-end and return the report.

    Split out from `viva.cli` so it's directly unit-testable with a fake
    `LLMClient` (see tests/test_phase0_demo.py) without going through
    Typer's runner or a real Ollama call.
    """
    timer = AnswerTimer(duration_seconds=duration_seconds)

    console.print(Panel(HARDCODED_QUESTION, title="Question", border_style="cyan"))
    minutes, seconds = divmod(int(duration_seconds), 60)
    console.print(f"[dim]You have {minutes:02d}:{seconds:02d} to answer.[/dim]")

    timer.start()
    answer = console.input("[bold]Your answer:[/bold] ")
    answer_time_seconds = timer.elapsed()

    with console.status("[dim]Evaluating (not counted against your time)...[/dim]"):
        with timer.excluding():
            call_result = llm_client.evaluate_answer(
                question=HARDCODED_QUESTION,
                ground_truth_context=GROUND_TRUTH_CONTEXT,
                user_answer=answer,
            )

    report = DemoReport(
        question=HARDCODED_QUESTION,
        answer=answer,
        evaluation=call_result.result,
        answer_time_seconds=answer_time_seconds,
        llm_latency_seconds=call_result.duration_seconds,
    )
    console.print(Panel(report.to_markdown(), title="Report", border_style="green"))
    return report
