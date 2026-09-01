import json
from unittest.mock import MagicMock

import pytest

from viva.llm_client import QUESTION_GEN_SYSTEM_PROMPT, OllamaClient
from viva.schemas import ClassificationResult


def _chat_response(content: str) -> dict:
    return {"message": {"content": content}}


@pytest.fixture
def client(monkeypatch):
    c = OllamaClient(model="test-model", temperature=0.3, host="http://localhost:11434")
    c._client = MagicMock()  # replace the real ollama.Client
    return c


# -- classify_answer (call #1) -----------------------------------------------

def test_valid_first_attempt(client):
    valid = json.dumps({"classification": "correct", "summary": "Good answer."})
    client._client.chat.return_value = _chat_response(valid)

    call_result = client.classify_answer("Q?", "context", "answer")

    assert call_result.result.classification == "correct"
    assert call_result.attempts == 1
    assert client._client.chat.call_count == 1


def test_repair_loop_recovers_on_second_attempt(client):
    malformed = "not json at all"
    valid = json.dumps({"classification": "partial", "summary": "Missed X.", "cited_file": "a.py:1"})
    client._client.chat.side_effect = [_chat_response(malformed), _chat_response(valid)]

    call_result = client.classify_answer("Q?", "context", "answer")

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

    call_result = client.classify_answer("Q?", "context", "answer")

    assert call_result.result.needs_review is True
    assert call_result.result.classification == "not_attempted"
    assert client._client.chat.call_count == 2


def test_ungrounded_incorrect_verdict_is_downgraded(client):
    """FR22: an 'incorrect' classification with no citation must be flagged
    needs_review, not surfaced as ungrounded criticism -- enforced at the
    application layer even if the model doesn't follow the system prompt."""
    ungrounded = json.dumps({"classification": "incorrect", "summary": "Wrong."})
    client._client.chat.return_value = _chat_response(ungrounded)

    call_result = client.classify_answer("Q?", "context", "answer")

    assert call_result.result.needs_review is True


def test_grounded_incorrect_verdict_passes_through(client):
    grounded = json.dumps(
        {"classification": "incorrect", "summary": "Wrong.", "cited_file": "src/x.py:10"}
    )
    client._client.chat.return_value = _chat_response(grounded)

    call_result = client.classify_answer("Q?", "context", "answer")

    assert call_result.result.needs_review is False


def test_classify_answer_schema_sent_to_model_excludes_needs_review(client):
    """docs/system-design/12-phase-7-evaluator-design.md's needs_review
    fix: the field is client-set only, so it must not even appear in the
    schema handed to the model's constrained decoding."""
    valid = json.dumps({"classification": "correct", "summary": "ok"})
    client._client.chat.return_value = _chat_response(valid)

    client.classify_answer("Q?", "context", "answer")

    schema_sent = client._client.chat.call_args.kwargs["format"]
    assert "needs_review" not in schema_sent["properties"]
    assert "needs_review" not in schema_sent.get("required", [])


def test_classify_answer_discards_model_supplied_needs_review_when_grounded(client):
    """Regression test for a real gemma4:e4b run: the model populated
    needs_review: true on its own even though the verdict was grounded
    (cited_file present, classification correct) -- must be discarded,
    not trusted, since needs_review is meant to be client-set only."""
    model_set_it_anyway = json.dumps(
        {
            "classification": "correct",
            "summary": "Good answer.",
            "cited_file": "src/x.py:10",
            "needs_review": True,
        }
    )
    client._client.chat.return_value = _chat_response(model_set_it_anyway)

    call_result = client.classify_answer("Q?", "context", "answer")

    assert call_result.result.needs_review is False


def test_classify_prompt_uses_labeled_sections(client):
    valid = json.dumps({"classification": "correct", "summary": "ok"})
    client._client.chat.return_value = _chat_response(valid)

    client.classify_answer("What does X do?", "def x(): ...", "It does X")

    first_call_messages = client._client.chat.call_args_list[0].kwargs["messages"]
    user_prompt = first_call_messages[1]["content"]
    assert "[QUESTION]" in user_prompt
    assert "[GROUND_TRUTH_CODE_CONTEXT]" in user_prompt
    assert "[USER_ANSWER]" in user_prompt


# -- generate_feedback (call #2) ---------------------------------------------

def _classification(**overrides) -> ClassificationResult:
    values = dict(classification="partial", summary="Missed the edge case.", cited_file="a.py:1")
    values.update(overrides)
    return ClassificationResult(**values)


def test_generate_feedback_valid_first_attempt(client):
    valid = json.dumps(
        {
            "did_well": ["Explained the happy path."],
            "missed": [{"point": "Didn't mention retries.", "cited_file": "a.py:1"}],
            "did_wrong": [],
            "improvement": "Read the retry logic in a.py.",
        }
    )
    client._client.chat.return_value = _chat_response(valid)

    call_result = client.generate_feedback("Q?", "context", "answer", _classification())

    assert call_result.result.did_well == ["Explained the happy path."]
    assert call_result.result.missed[0].point == "Didn't mention retries."
    assert call_result.attempts == 1


def test_generate_feedback_includes_prior_verdict_in_prompt(client):
    valid = json.dumps({"improvement": "n/a"})
    client._client.chat.return_value = _chat_response(valid)

    client.generate_feedback("Q?", "context", "answer", _classification())

    user_prompt = client._client.chat.call_args.kwargs["messages"][1]["content"]
    assert "[VERDICT_ALREADY_GIVEN]" in user_prompt
    assert "partial" in user_prompt
    assert "Missed the edge case." in user_prompt


def test_generate_feedback_drops_uncited_missed_entries(client):
    """FR22: an uncited 'missed'/'did_wrong' entry is dropped at the
    application layer, same discipline as classify_answer's cited_file
    enforcement."""
    valid = json.dumps(
        {
            "did_well": [],
            "missed": [
                {"point": "Cited point.", "cited_file": "a.py:1"},
                {"point": "Uncited point."},
            ],
            "did_wrong": [{"point": "Uncited wrong point."}],
            "improvement": "n/a",
        }
    )
    client._client.chat.return_value = _chat_response(valid)

    call_result = client.generate_feedback("Q?", "context", "answer", _classification())

    assert len(call_result.result.missed) == 1
    assert call_result.result.missed[0].point == "Cited point."
    assert call_result.result.did_wrong == []


def test_generate_feedback_needs_review_when_dropping_empties_critical_verdict(client):
    """Dropping every uncited entry on a 'partial'/'incorrect' verdict
    leaves nothing to substantiate the criticism -- must be flagged
    needs_review rather than silently look like a clean pass."""
    valid = json.dumps(
        {
            "did_well": [],
            "missed": [{"point": "Uncited point."}],
            "did_wrong": [],
            "improvement": "n/a",
        }
    )
    client._client.chat.return_value = _chat_response(valid)

    call_result = client.generate_feedback(
        "Q?", "context", "answer", _classification(classification="incorrect")
    )

    assert call_result.result.missed == []
    assert call_result.result.needs_review is True


def test_generate_feedback_correct_verdict_not_forced_needs_review_when_empty(client):
    """A 'correct' verdict legitimately has nothing in missed/did_wrong --
    that's not the same failure mode as a critical verdict losing its
    only grounding."""
    valid = json.dumps({"did_well": ["Nailed it."], "missed": [], "did_wrong": [], "improvement": "n/a"})
    client._client.chat.return_value = _chat_response(valid)

    call_result = client.generate_feedback(
        "Q?", "context", "answer", _classification(classification="correct", cited_file=None)
    )

    assert call_result.result.needs_review is False


def test_generate_feedback_schema_sent_to_model_excludes_needs_review(client):
    valid = json.dumps({"improvement": "n/a"})
    client._client.chat.return_value = _chat_response(valid)

    client.generate_feedback("Q?", "context", "answer", _classification())

    schema_sent = client._client.chat.call_args.kwargs["format"]
    assert "needs_review" not in schema_sent["properties"]
    assert "needs_review" not in schema_sent.get("required", [])


def test_generate_feedback_discards_model_supplied_needs_review_when_grounded(client):
    """Same regression as classify_answer's: a real run showed the model
    setting needs_review: true unprompted even when every missed/
    did_wrong entry was properly cited -- must be discarded."""
    model_set_it_anyway = json.dumps(
        {
            "did_well": [],
            "missed": [{"point": "Cited point.", "cited_file": "a.py:1"}],
            "did_wrong": [],
            "improvement": "n/a",
            "needs_review": True,
        }
    )
    client._client.chat.return_value = _chat_response(model_set_it_anyway)

    call_result = client.generate_feedback(
        "Q?", "context", "answer", _classification(classification="partial")
    )

    assert call_result.result.needs_review is False


def test_generate_feedback_falls_back_after_two_failures(client):
    client._client.chat.side_effect = [
        _chat_response("garbage"),
        _chat_response("still garbage"),
    ]

    call_result = client.generate_feedback("Q?", "context", "answer", _classification())

    assert call_result.result.needs_review is True
    assert call_result.result.did_well == []
    assert client._client.chat.call_count == 2


# -- other calls (unaffected by the split) -----------------------------------

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


def test_question_gen_system_prompt_constrains_length_and_clause_count():
    # Regression guard for the verbose, multi-clause questions observed
    # on a real click run ("...especially when encountering a Group
    # command with chain enabled, and X, and Y..."). The system prompt
    # is the actual lever here -- target_tokens=80 gives ~240 tokens of
    # num_predict headroom, nowhere near tight enough to force brevity on
    # its own, so the constraint has to be explicit in the instructions.
    assert "ONE clause" in QUESTION_GEN_SYSTEM_PROMPT
    assert "15-25 words" in QUESTION_GEN_SYSTEM_PROMPT


def test_generate_question_builds_labeled_sections(client):
    client._client.chat.return_value = _chat_response("How does this module handle a failed retry?")

    result = client.generate_question(
        category="error_handling", target_module="payments", grounding_context="def retry(): ..."
    )

    assert result == "How does this module handle a failed retry?"
    call_kwargs = client._client.chat.call_args.kwargs
    assert call_kwargs["think"] is False
    user_prompt = call_kwargs["messages"][1]["content"]
    assert "[CATEGORY]\nerror_handling" in user_prompt
    assert "[TARGET_MODULE]\npayments" in user_prompt
    assert "[CODE_CONTEXT]\ndef retry(): ..." in user_prompt


def test_generate_question_labels_project_level_target_module(client):
    client._client.chat.return_value = _chat_response("What's the overall architecture?")

    client.generate_question(category="architecture", target_module=None, grounding_context="ctx")

    user_prompt = client._client.chat.call_args.kwargs["messages"][1]["content"]
    assert "[TARGET_MODULE]\n(project-level)" in user_prompt


def test_generate_question_includes_target_file_section_when_present(client):
    client._client.chat.return_value = _chat_response("How does core.py handle retries?")

    client.generate_question(
        category="implementation_detail", target_module="src",
        grounding_context="def retry(): ...", target_file="src/core.py",
    )

    user_prompt = client._client.chat.call_args.kwargs["messages"][1]["content"]
    assert "[TARGET_FILE]\nsrc/core.py" in user_prompt


def test_generate_question_omits_target_file_section_when_absent(client):
    client._client.chat.return_value = _chat_response("How does this module handle a failed retry?")

    client.generate_question(category="error_handling", target_module="payments", grounding_context="ctx")

    user_prompt = client._client.chat.call_args.kwargs["messages"][1]["content"]
    assert "[TARGET_FILE]" not in user_prompt


def test_generate_question_includes_avoid_repeating_section_when_present(client):
    client._client.chat.return_value = _chat_response("A fresh question.")

    client.generate_question(
        category="architecture", target_module="core", grounding_context="ctx",
        avoid_questions=["Why does X use a switch expression?", "What happens if Y is null?"],
    )

    user_prompt = client._client.chat.call_args.kwargs["messages"][1]["content"]
    assert "[AVOID_REPEATING]" in user_prompt
    assert "Why does X use a switch expression?" in user_prompt
    assert "What happens if Y is null?" in user_prompt


def test_generate_question_omits_avoid_repeating_section_when_absent(client):
    client._client.chat.return_value = _chat_response("A fresh question.")

    client.generate_question(category="architecture", target_module="core", grounding_context="ctx")

    user_prompt = client._client.chat.call_args.kwargs["messages"][1]["content"]
    assert "[AVOID_REPEATING]" not in user_prompt


def test_generate_question_omits_avoid_repeating_section_when_empty_list(client):
    client._client.chat.return_value = _chat_response("A fresh question.")

    client.generate_question(
        category="architecture", target_module="core", grounding_context="ctx", avoid_questions=[],
    )

    user_prompt = client._client.chat.call_args.kwargs["messages"][1]["content"]
    assert "[AVOID_REPEATING]" not in user_prompt


def test_get_context_window_parses_family_namespaced_key(client):
    client._client.show.return_value = {"model_info": {"gemma3.context_length": 8192, "other.key": "x"}}

    assert client.get_context_window() == 8192


def test_get_context_window_returns_none_on_missing_info(client):
    client._client.show.return_value = {}

    assert client.get_context_window() is None


def test_get_context_window_returns_none_on_error(client):
    client._client.show.side_effect = RuntimeError("model not found")

    assert client.get_context_window() is None
