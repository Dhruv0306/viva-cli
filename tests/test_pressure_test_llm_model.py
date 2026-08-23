import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pressure_test_llm_model import (  # noqa: E402
    RepetitionOutcome,
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


def _eval(classification, cited_file=None, needs_review=False):
    return EvaluationResult(
        classification=classification,
        summary="s",
        cited_file=cited_file,
        needs_review=needs_review,
    )


def _outcome(classification, cited_file=None, needs_review=False):
    return RepetitionOutcome(result=_eval(classification, cited_file, needs_review))


def _error_outcome(message="boom"):
    return RepetitionOutcome(
        result=_eval("not_attempted", needs_review=True),
        is_call_error=True,
        error_message=message,
    )


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
    outcomes = run_repetitions(client, SAMPLE, repetitions=5)
    assert len(outcomes) == 5
    assert all(not o.is_call_error for o in outcomes)
    assert client.evaluate_answer.call_count == 5
    client.evaluate_answer.assert_called_with(
        question="q", ground_truth_context="ctx", user_answer="a"
    )


def test_run_repetitions_tags_exceptions_as_call_errors_not_model_results(mocker):
    # This is the exact bug the harness had: a client-side exception (e.g. a
    # timeout) must never be indistinguishable from the model genuinely
    # returning not_attempted.
    client = mocker.Mock()
    client.evaluate_answer.side_effect = TimeoutError("read timed out")
    outcomes = run_repetitions(client, SAMPLE, repetitions=3)
    assert len(outcomes) == 3
    assert all(o.is_call_error for o in outcomes)
    assert all("read timed out" in o.error_message for o in outcomes)


def test_run_repetitions_mixes_valid_and_error_outcomes(mocker):
    client = mocker.Mock()
    client.evaluate_answer.side_effect = [
        LLMCallResult(result=_eval("correct"), duration_seconds=0.1, attempts=1),
        TimeoutError("boom"),
        LLMCallResult(result=_eval("correct"), duration_seconds=0.1, attempts=1),
    ]
    outcomes = run_repetitions(client, SAMPLE, repetitions=3)
    assert [o.is_call_error for o in outcomes] == [False, True, False]


def test_compute_sample_stats_perfect_stability():
    outcomes = [_outcome("partial", "f.py:1")] * 5
    stats = compute_sample_stats(SAMPLE, outcomes)
    assert stats.modal_classification == "partial"
    assert stats.stability_rate == 1.0
    assert stats.citation_eligible_runs == 5
    assert stats.citation_present_runs == 5
    assert stats.call_error_runs == 0


def test_compute_sample_stats_flags_instability():
    # Mirrors the exact Phase 0 finding: same answer, partial vs incorrect.
    outcomes = [
        _outcome("partial", "f.py:1"),
        _outcome("incorrect", "f.py:1"),
        _outcome("partial", "f.py:1"),
        _outcome("partial", "f.py:1"),
    ]
    stats = compute_sample_stats(SAMPLE, outcomes)
    assert stats.modal_classification == "partial"
    assert stats.stability_rate == 0.75


def test_compute_sample_stats_citation_compliance_partial():
    outcomes = [
        _outcome("incorrect", "f.py:1"),
        _outcome("incorrect", None),
        _outcome("incorrect", None),
    ]
    stats = compute_sample_stats(SAMPLE, outcomes)
    assert stats.citation_eligible_runs == 3
    assert stats.citation_present_runs == 1


def test_compute_sample_stats_correct_and_not_attempted_are_citation_exempt():
    outcomes = [_outcome("correct"), _outcome("not_attempted")]
    stats = compute_sample_stats(SAMPLE, outcomes)
    assert stats.citation_eligible_runs == 0
    assert stats.citation_present_runs == 0


def test_compute_sample_stats_excludes_call_errors_from_stability_and_denominator():
    # 3 clean 'correct' verdicts + 2 call errors. Stability must be computed
    # over the 3 valid runs only (100%), not diluted to 60% by errors that
    # say nothing about the model, and the 2 errors must be visible as
    # call_error_runs rather than silently vanishing.
    outcomes = [_outcome("correct")] * 3 + [_error_outcome(), _error_outcome()]
    stats = compute_sample_stats(SAMPLE, outcomes)
    assert stats.call_error_runs == 2
    assert len(stats.classifications) == 3
    assert stats.stability_rate == 1.0
    assert stats.modal_classification == "correct"


def test_compute_sample_stats_all_calls_erroring_does_not_claim_full_stability():
    outcomes = [_error_outcome(), _error_outcome()]
    stats = compute_sample_stats(SAMPLE, outcomes)
    assert stats.call_error_runs == 2
    assert stats.classifications == []
    assert stats.stability_rate == 0.0
    assert stats.accuracy_rate is None


def test_compute_sample_stats_tracks_needs_review_separately_from_call_errors():
    outcomes = [
        _outcome("not_attempted", needs_review=True),  # model's own repair-loop exhaustion
        _outcome("correct"),
        _error_outcome(),  # harness-level failure, not the model's doing
    ]
    stats = compute_sample_stats(SAMPLE, outcomes)
    assert stats.needs_review_runs == 1
    assert stats.call_error_runs == 1


def test_compute_sample_stats_accuracy_catches_confident_wrong_answers():
    # This is the mistral-nemo:12b finding: majority-correct classification
    # of a sample whose expected label is 'incorrect' is high 'stability'
    # but low accuracy, and accuracy is the metric that must catch it.
    incorrect_sample = {**SAMPLE, "expected_classification": "incorrect"}
    outcomes = [_outcome("correct")] * 6 + [_outcome("incorrect", "f.py:1")] * 4
    stats = compute_sample_stats(incorrect_sample, outcomes)
    assert stats.modal_classification == "correct"
    assert stats.stability_rate == 0.6
    assert stats.accuracy_rate == pytest.approx(0.4)  # only the 4 'incorrect' runs are accurate


def test_compute_model_stats_aggregates_across_samples():
    sample_a = {**SAMPLE, "id": "a"}
    sample_b = {**SAMPLE, "id": "b"}
    outcomes_by_id = {
        "a": [_outcome("partial", "f.py:1")] * 4,  # fully stable, fully cited
        "b": [_outcome("incorrect", None), _outcome("incorrect", "f.py:2")],  # 50% cited
    }
    stats = compute_model_stats("test-model", [sample_a, sample_b], outcomes_by_id)
    assert stats.model == "test-model"
    assert stats.mean_stability_rate == pytest.approx(1.0)
    # 4/4 cited for sample a + 1/2 cited for sample b = 5/6 overall.
    assert stats.citation_compliance_rate == pytest.approx(5 / 6)


def test_compute_model_stats_totals_needs_review_and_call_errors():
    sample_a = {**SAMPLE, "id": "a"}
    sample_b = {**SAMPLE, "id": "b"}
    outcomes_by_id = {
        "a": [_outcome("correct", needs_review=True), _error_outcome()],
        "b": [_outcome("correct")],
    }
    stats = compute_model_stats("test-model", [sample_a, sample_b], outcomes_by_id)
    assert stats.total_needs_review_runs == 1
    assert stats.total_call_error_runs == 1


def test_mean_accuracy_rate_penalizes_total_failure_sample_instead_of_excluding_it():
    # This is the exact qwen3.5:latest bug: one sample where every call
    # errors must drag the mean down (accuracy_rate=None -> counted as 0),
    # not be silently dropped from the denominator and inflate the mean.
    ok_sample = {**SAMPLE, "id": "ok", "expected_classification": "correct"}
    failed_sample = {**SAMPLE, "id": "failed", "expected_classification": "incorrect"}
    outcomes_by_id = {
        "ok": [_outcome("correct")] * 4,  # 100% accuracy
        "failed": [_error_outcome()] * 4,  # every call errored -> accuracy_rate is None
    }
    stats = compute_model_stats("test-model", [ok_sample, failed_sample], outcomes_by_id)
    assert stats.sample_stats[1].accuracy_rate is None
    # Correct: (100% + 0%) / 2 = 50%. Buggy old behavior would have
    # excluded the None and reported 100%.
    assert stats.mean_accuracy_rate == pytest.approx(0.5)


def test_citation_compliance_rate_is_none_when_no_eligible_verdicts():
    sample_a = {**SAMPLE, "id": "a"}
    outcomes_by_id = {"a": [_outcome("correct"), _outcome("not_attempted")]}
    stats = compute_model_stats("test-model", [sample_a], outcomes_by_id)
    assert stats.citation_compliance_rate is None
