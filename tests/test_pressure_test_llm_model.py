import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pressure_test_llm_model import (  # noqa: E402
    compute_model_stats,
    compute_sample_stats,
    load_samples,
    run_repetitions,
)
from viva.llm_client import LLMCallResult  # noqa: E402
from viva.schemas import EvaluationResult  # noqa: E402


SAMPLE = {
    "id": "sample-1",
    "question": "q",
    "ground_truth_context": "ctx",
    "user_answer": "a",
    "expected_classification": "partial",
}


def _eval(classification, cited_file=None):
    return EvaluationResult(classification=classification, summary="s", cited_file=cited_file)


def test_load_samples_returns_fixture_set():
    samples = load_samples()
    assert len(samples) >= 3
    ids = {s["id"] for s in samples}
    expected = {s["expected_classification"] for s in samples}
    assert "correct" in expected
    assert "partial" in expected
    assert "incorrect" in expected
    assert "not_attempted" in expected
    assert len(ids) == len(samples)  # no duplicate ids


def test_run_repetitions_calls_client_n_times(mocker):
    client = mocker.Mock()
    client.evaluate_answer.return_value = LLMCallResult(
        result=_eval("correct"), duration_seconds=0.1, attempts=1
    )
    results = run_repetitions(client, SAMPLE, repetitions=5)
    assert len(results) == 5
    assert client.evaluate_answer.call_count == 5
    client.evaluate_answer.assert_called_with(
        question="q", ground_truth_context="ctx", user_answer="a"
    )


def test_compute_sample_stats_perfect_stability():
    results = [_eval("partial", "f.py:1")] * 5
    stats = compute_sample_stats(SAMPLE, results)
    assert stats.modal_classification == "partial"
    assert stats.stability_rate == 1.0
    assert stats.citation_eligible_runs == 5
    assert stats.citation_present_runs == 5


def test_compute_sample_stats_flags_instability():
    # Mirrors the exact Phase 0 finding: same answer, partial vs incorrect.
    results = [
        _eval("partial", "f.py:1"),
        _eval("incorrect", "f.py:1"),
        _eval("partial", "f.py:1"),
        _eval("partial", "f.py:1"),
    ]
    stats = compute_sample_stats(SAMPLE, results)
    assert stats.modal_classification == "partial"
    assert stats.stability_rate == 0.75


def test_compute_sample_stats_citation_compliance_partial():
    results = [
        _eval("incorrect", "f.py:1"),
        _eval("incorrect", None),
        _eval("incorrect", None),
    ]
    stats = compute_sample_stats(SAMPLE, results)
    assert stats.citation_eligible_runs == 3
    assert stats.citation_present_runs == 1


def test_compute_sample_stats_correct_and_not_attempted_are_citation_exempt():
    results = [_eval("correct"), _eval("not_attempted")]
    stats = compute_sample_stats(SAMPLE, results)
    assert stats.citation_eligible_runs == 0
    assert stats.citation_present_runs == 0


def test_compute_model_stats_aggregates_across_samples():
    sample_a = {**SAMPLE, "id": "a"}
    sample_b = {**SAMPLE, "id": "b"}
    results_by_id = {
        "a": [_eval("partial", "f.py:1")] * 4,  # fully stable, fully cited
        "b": [_eval("incorrect", None), _eval("incorrect", "f.py:2")],  # 50% cited
    }
    stats = compute_model_stats("test-model", [sample_a, sample_b], results_by_id)
    assert stats.model == "test-model"
    assert stats.mean_stability_rate == pytest.approx(1.0)
    # 4/4 cited for sample a + 1/2 cited for sample b = 5/6 overall.
    assert stats.citation_compliance_rate == pytest.approx(5 / 6)


def test_citation_compliance_rate_is_none_when_no_eligible_verdicts():
    sample_a = {**SAMPLE, "id": "a"}
    results_by_id = {"a": [_eval("correct"), _eval("not_attempted")]}
    stats = compute_model_stats("test-model", [sample_a], results_by_id)
    assert stats.citation_compliance_rate is None
