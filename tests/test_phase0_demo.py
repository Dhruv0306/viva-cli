import time
from unittest.mock import patch

from rich.console import Console

from viva.llm_client import LLMCallResult, LLMClient
from viva.phase0_demo import run_demo
from viva.schemas import EvaluationResult


class FakeSlowLLMClient(LLMClient):
    """Simulates real LLM latency so the timer-exclusion behavior is
    exercised end-to-end, per Phase 0 exit criteria (docs/plan.md)."""

    def __init__(self, sleep_seconds: float = 0.2) -> None:
        self.sleep_seconds = sleep_seconds
        self.calls = []

    def evaluate_answer(self, question, ground_truth_context, user_answer):
        self.calls.append((question, ground_truth_context, user_answer))
        start = time.monotonic()
        time.sleep(self.sleep_seconds)
        duration = time.monotonic() - start
        result = EvaluationResult(
            classification="correct",
            summary="Correctly explained monotonic time and exclusion.",
            cited_file="src/viva/timer.py:33",
        )
        return LLMCallResult(result=result, duration_seconds=duration, attempts=1)

    def summarize_file(self, path, language, content_excerpt, target_tokens):
        raise NotImplementedError  # not exercised by the Phase 0 demo

    def reduce(self, label, summaries, target_tokens):
        raise NotImplementedError  # not exercised by the Phase 0 demo

    def generate_question(self, category, target_module, grounding_context):
        raise NotImplementedError  # not exercised by the Phase 0 demo


def test_run_demo_end_to_end_excludes_llm_latency():
    # sleep_seconds is deliberately higher than the >= 0.2 assertion below
    # needs -- this test predates Phase 3 and isn't part of its scope, but
    # was flaky on Windows: a strict time.sleep(0.2)/assert >= 0.2 with
    # zero slack is fragile against Windows' coarser timer/sleep
    # resolution, which can occasionally measure a hair under 0.2s for a
    # requested 0.2s sleep. 50ms of headroom comfortably absorbs that
    # without weakening what the assertion is actually checking (that
    # LLM latency is excluded from the answer clock, not that it's
    # precisely 0.2s).
    fake_client = FakeSlowLLMClient(sleep_seconds=0.25)
    console = Console(record=True)

    with patch.object(Console, "input", return_value="It avoids wall-clock jumps."):
        report = run_demo(llm_client=fake_client, duration_seconds=60, console=console)

    assert len(fake_client.calls) == 1
    assert report.evaluation.classification == "correct"
    assert report.llm_latency_seconds >= 0.2
    # The (near-instant, mocked) answer-capture step should be nowhere near
    # the 0.2s LLM latency -- this is the concrete FR17 assertion for the
    # walking skeleton, exercised through the real AnswerTimer + real sleep.
    assert report.answer_time_seconds < 0.1


def test_run_demo_displays_the_code_snippet_it_grounds_on():
    """Regression test: the question text references 'the excluding()
    context manager below', so the snippet must actually be printed to the
    console -- not just sent silently to the LLM as grounding context."""
    fake_client = FakeSlowLLMClient(sleep_seconds=0.01)
    console = Console(record=True)

    with patch.object(Console, "input", return_value="some answer"):
        run_demo(llm_client=fake_client, duration_seconds=60, console=console)

    rendered = console.export_text()
    assert "def excluding(self)" in rendered
    assert "src/viva/timer.py" in rendered


def test_run_demo_report_contains_grounded_citation():
    fake_client = FakeSlowLLMClient(sleep_seconds=0.01)
    console = Console(record=True)

    with patch.object(Console, "input", return_value="some answer"):
        report = run_demo(llm_client=fake_client, duration_seconds=60, console=console)

    markdown = report.to_markdown()
    assert "timer.py:33" in markdown
    assert report.evaluation.needs_review is False
