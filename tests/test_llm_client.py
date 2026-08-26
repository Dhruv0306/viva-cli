import json
from unittest.mock import MagicMock

import pytest

from viva.llm_client import OllamaClient


def _chat_response(content: str) -> dict:
    return {"message": {"content": content}}


@pytest.fixture
def client(monkeypatch):
    c = OllamaClient(model="test-model", temperature=0.3, host="http://localhost:11434")
    c._client = MagicMock()  # replace the real ollama.Client
    return c


def test_valid_first_attempt(client):
    valid = json.dumps({"classification": "correct", "summary": "Good answer."})
    client._client.chat.return_value = _chat_response(valid)

    call_result = client.evaluate_answer("Q?", "context", "answer")

    assert call_result.result.classification == "correct"
    assert call_result.attempts == 1
    assert client._client.chat.call_count == 1


def test_repair_loop_recovers_on_second_attempt(client):
    malformed = "not json at all"
    valid = json.dumps({"classification": "partial", "summary": "Missed X.", "cited_file": "a.py:1"})
    client._client.chat.side_effect = [_chat_response(malformed), _chat_response(valid)]

    call_result = client.evaluate_answer("Q?", "context", "answer")

    assert call_result.result.classification == "partial"
    assert call_result.attempts == 2
    assert client._client.chat.call_count == 2
    # second call should include the repair instruction referencing the error
    second_call_messages = client._client.chat.call_args_list[1].kwargs["messages"]
    assert any("failed schema validation" in m["content"] for m in second_call_messages)


def test_falls_back_to_needs_review_after_two_failures(client):
    client._client.chat.side_effect = [
        _chat_response("garbage"),
        _chat_response("still garbage"),
    ]

    call_result = client.evaluate_answer("Q?", "context", "answer")

    assert call_result.result.needs_review is True
    assert call_result.result.classification == "not_attempted"
    assert client._client.chat.call_count == 2


def test_ungrounded_incorrect_verdict_is_downgraded(client):
    """FR22: an 'incorrect' classification with no citation must be flagged
    needs_review, not surfaced as ungrounded criticism -- enforced at the
    application layer even if the model doesn't follow the system prompt."""
    ungrounded = json.dumps({"classification": "incorrect", "summary": "Wrong."})
    client._client.chat.return_value = _chat_response(ungrounded)

    call_result = client.evaluate_answer("Q?", "context", "answer")

    assert call_result.result.needs_review is True


def test_grounded_incorrect_verdict_passes_through(client):
    grounded = json.dumps(
        {"classification": "incorrect", "summary": "Wrong.", "cited_file": "src/x.py:10"}
    )
    client._client.chat.return_value = _chat_response(grounded)

    call_result = client.evaluate_answer("Q?", "context", "answer")

    assert call_result.result.needs_review is False


def test_prompt_uses_labeled_sections(client):
    valid = json.dumps({"classification": "correct", "summary": "ok"})
    client._client.chat.return_value = _chat_response(valid)

    client.evaluate_answer("What does X do?", "def x(): ...", "It does X")

    first_call_messages = client._client.chat.call_args_list[0].kwargs["messages"]
    user_prompt = first_call_messages[1]["content"]
    assert "[QUESTION]" in user_prompt
    assert "[GROUND_TRUTH_CODE_CONTEXT]" in user_prompt
    assert "[USER_ANSWER]" in user_prompt


def test_summarize_file_returns_stripped_text(client):
    client._client.chat.return_value = _chat_response("  A file that adds two numbers.  \n")

    summary = client.summarize_file(
        path="src/foo.py", language="python", content_excerpt="def foo(a, b): ...", target_tokens=150
    )

    assert summary == "A file that adds two numbers."
    call_kwargs = client._client.chat.call_args.kwargs
    assert call_kwargs["options"]["num_predict"] == int(150 * 3)
    assert call_kwargs["think"] is False
    messages = call_kwargs["messages"]
    assert "src/foo.py" in messages[1]["content"]
    assert "python" in messages[1]["content"]


def test_summarize_file_empty_response_returns_placeholder_not_blank(client, caplog):
    # A thinking-capable model can exhaust num_predict on hidden reasoning
    # and return empty visible content -- must never propagate a blank
    # string silently into the Project Profile (that's what produced the
    # "please provide the summaries" cascade against a real model).
    client._client.chat.return_value = _chat_response("")

    with caplog.at_level("WARNING"):
        summary = client.summarize_file(
            path="src/foo.py", language="python", content_excerpt="def foo(): ...", target_tokens=150
        )

    assert summary != ""
    assert "unavailable" in summary
    assert any("Empty content" in r.message for r in caplog.records)


def test_reduce_combines_summaries_into_one_prompt(client):
    client._client.chat.return_value = _chat_response("Combined summary.")

    result = client.reduce("Module: src", ["Summary A.", "Summary B."], target_tokens=200)

    assert result == "Combined summary."
    call_kwargs = client._client.chat.call_args.kwargs
    assert call_kwargs["think"] is False
    user_prompt = call_kwargs["messages"][1]["content"]
    assert "Module: src" in user_prompt
    assert "Summary A." in user_prompt
    assert "Summary B." in user_prompt


def test_get_context_window_parses_family_namespaced_key(client):
    client._client.show.return_value = {"model_info": {"gemma3.context_length": 8192, "other.key": "x"}}

    assert client.get_context_window() == 8192


def test_get_context_window_returns_none_on_missing_info(client):
    client._client.show.return_value = {}

    assert client.get_context_window() is None


def test_get_context_window_returns_none_on_error(client):
    client._client.show.side_effect = RuntimeError("model not found")

    assert client.get_context_window() is None
