import pytest
from pydantic import ValidationError

from viva.schemas import EvaluationResult


def test_valid_correct_needs_no_citation():
    result = EvaluationResult(classification="correct", summary="Nailed it.")
    assert result.cited_file is None
    assert result.needs_review is False


def test_valid_partial_with_citation():
    result = EvaluationResult(
        classification="partial",
        summary="Got the mechanism but not the edge case.",
        cited_file="src/viva/timer.py:33",
    )
    assert result.cited_file == "src/viva/timer.py:33"


def test_invalid_classification_rejected():
    with pytest.raises(ValidationError):
        EvaluationResult(classification="sort-of", summary="x")


def test_empty_summary_rejected():
    with pytest.raises(ValidationError):
        EvaluationResult(classification="correct", summary="")


def test_missing_required_fields_rejected():
    with pytest.raises(ValidationError):
        EvaluationResult(classification="correct")  # missing summary


def test_json_schema_is_generatable():
    """Sanity check that the schema is well-formed enough to hand to
    Ollama's `format=` constrained-decoding parameter."""
    schema = EvaluationResult.model_json_schema()
    assert schema["type"] == "object"
    assert "classification" in schema["properties"]
    assert "summary" in schema["properties"]
