import pytest
from pydantic import ValidationError

from viva.schemas import ClassificationResult, EvaluationFeedback, EvaluationRecord, MissedPoint


def test_valid_correct_needs_no_citation():
    result = ClassificationResult(classification="correct", summary="Nailed it.")
    assert result.cited_file is None
    assert result.needs_review is False


def test_valid_partial_with_citation():
    result = ClassificationResult(
        classification="partial",
        summary="Got the mechanism but not the edge case.",
        cited_file="src/viva/timer.py:33",
    )
    assert result.cited_file == "src/viva/timer.py:33"


def test_invalid_classification_rejected():
    with pytest.raises(ValidationError):
        ClassificationResult(classification="sort-of", summary="x")


def test_empty_summary_rejected():
    with pytest.raises(ValidationError):
        ClassificationResult(classification="correct", summary="")


def test_missing_required_fields_rejected():
    with pytest.raises(ValidationError):
        ClassificationResult(classification="correct")  # missing summary


def test_json_schema_is_generatable():
    """Sanity check that the schema is well-formed enough to hand to
    Ollama's `format=` constrained-decoding parameter."""
    schema = ClassificationResult.model_json_schema()
    assert schema["type"] == "object"
    assert "classification" in schema["properties"]
    assert "summary" in schema["properties"]


def test_evaluation_feedback_defaults_to_empty_lists():
    feedback = EvaluationFeedback(improvement="Read more about X.")
    assert feedback.did_well == []
    assert feedback.missed == []
    assert feedback.did_wrong == []
    assert feedback.needs_review is False


def test_evaluation_feedback_json_schema_is_generatable():
    schema = EvaluationFeedback.model_json_schema()
    assert schema["type"] == "object"
    assert "did_well" in schema["properties"]
    assert "missed" in schema["properties"]
    assert "did_wrong" in schema["properties"]
    assert "improvement" in schema["properties"]


def test_missed_point_citation_is_optional():
    point = MissedPoint(point="Didn't mention retry backoff.")
    assert point.cited_file is None


def test_evaluation_record_from_calls_merges_both_calls():
    classification = ClassificationResult(
        classification="partial", summary="Mostly right.", cited_file="a.py:1",
    )
    feedback = EvaluationFeedback(
        did_well=["Explained the happy path."],
        missed=[MissedPoint(point="Didn't mention retry backoff.", cited_file="a.py:1")],
        improvement="Look at how retries are scheduled.",
    )

    record = EvaluationRecord.from_calls(classification, feedback)

    assert record.classification == "partial"
    assert record.summary == "Mostly right."
    assert record.cited_file == "a.py:1"
    assert record.did_well == ["Explained the happy path."]
    assert record.missed[0].point == "Didn't mention retry backoff."
    assert record.improvement == "Look at how retries are scheduled."
    assert record.needs_review is False


def test_evaluation_record_from_calls_needs_review_if_either_call_flagged_it():
    classification = ClassificationResult(classification="correct", summary="Ok.", needs_review=True)
    feedback = EvaluationFeedback(improvement="n/a")

    record = EvaluationRecord.from_calls(classification, feedback)

    assert record.needs_review is True
