"""Tests for viva.evaluator.Evaluator against real SessionStore/VectorStore
(both file-backed, cheap against a tmp_path) and a fully controllable
FakeLLMClient -- same "test real behavior where it's cheap" approach as
test_indexer_store.py/test_orchestrator.py.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from viva.evaluator import Evaluator
from viva.indexer.models import Chunk
from viva.indexer.store import VectorStore, collection_name
from viva.llm_client import LLMCallResult, LLMClient
from viva.questiongen.models import QuestionPlanItem
from viva.schemas import ClassificationResult, EvaluationFeedback
from viva.storage import SessionStore


class _FakeLLMClient(LLMClient):
    """classify_answer is synchronous and deterministic; generate_feedback
    can be told to block on an Event so tests can deterministically
    control worker-thread timing instead of racing real sleeps."""

    def __init__(self):
        self.classify_calls = []
        self.feedback_calls = []
        self.classification_to_return = ClassificationResult(
            classification="partial", summary="Missed the edge case.", cited_file="a.py:1",
        )
        self.feedback_to_return = EvaluationFeedback(
            did_well=["Explained the happy path."],
            missed=[],
            did_wrong=[],
            improvement="Look at retry handling.",
        )
        self.feedback_block_event: threading.Event | None = None

    def classify_answer(self, question, ground_truth_context, user_answer):
        self.classify_calls.append((question, ground_truth_context, user_answer))
        return LLMCallResult(result=self.classification_to_return, duration_seconds=0.0, attempts=1)

    def generate_feedback(self, question, ground_truth_context, user_answer, classification):
        if self.feedback_block_event is not None:
            self.feedback_block_event.wait(timeout=5)
        self.feedback_calls.append((question, ground_truth_context, user_answer, classification))
        return LLMCallResult(result=self.feedback_to_return, duration_seconds=0.0, attempts=1)

    def summarize_file(self, path, language, content_excerpt, target_tokens):
        raise NotImplementedError

    def reduce(self, label, summaries, target_tokens):
        raise NotImplementedError

    def generate_question(self, category, target_module, grounding_context, target_file=None, avoid_questions=None):
        raise NotImplementedError


def _chunk(id: str, text: str, filepath: str = "a.py") -> Chunk:
    return Chunk(
        id=id, text=text, filepath=filepath, module="core", symbol_name="foo",
        kind="function", parse_method="ast", language="python", start_line=1, end_line=3,
    )


@pytest.fixture
def session_store(tmp_path):
    s = SessionStore(str(tmp_path / "viva.db"))
    yield s
    s.close()


@pytest.fixture
def vector_store(tmp_path):
    return VectorStore(str(tmp_path / "chroma"))


@pytest.fixture
def collection(vector_store):
    name = collection_name("owner/repo", "abc123")
    vector_store.upsert_chunks(
        name, [_chunk("c1", "def foo(): ...")], [[1.0, 0.0]],
    )
    return name


def _seed_answered_question(session_store, session_id="sess1", question_id="q1", grounding=("c1",)):
    session_store.create_session(session_id, "https://github.com/o/r", None, None, 1800)
    session_store.save_plan(
        session_id, [QuestionPlanItem(id=question_id, category="architecture", target_module=None)]
    )
    session_store.record_question_asked(session_id, question_id, "How does X work?", list(grounding))
    session_store.record_answer(session_id, question_id, "It works by doing Y.")


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# -- classify() ---------------------------------------------------------------

def test_classify_before_bind_session_raises(session_store, vector_store):
    evaluator = Evaluator(session_store, vector_store, _FakeLLMClient())

    with pytest.raises(RuntimeError, match="bind_session"):
        evaluator.classify("q1", "an answer")


def test_classify_returns_none_for_unknown_question(session_store, vector_store, collection):
    evaluator = Evaluator(session_store, vector_store, _FakeLLMClient())
    session_store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    evaluator.bind_session("sess1", collection)

    assert evaluator.classify("nope", "an answer") is None
    evaluator.flush(timeout=2.0)


def test_classify_persists_classification_immediately(session_store, vector_store, collection):
    _seed_answered_question(session_store)
    llm_client = _FakeLLMClient()
    evaluator = Evaluator(session_store, vector_store, llm_client)
    evaluator.bind_session("sess1", collection)

    result = evaluator.classify("q1", "It works by doing Y.")

    assert result == "partial"
    record = session_store.get_qa_record("sess1", "q1")
    assert record.eval_status in ("classified", "feedback_pending", "complete", "needs_review")
    persisted = json.loads(record.eval_json)
    assert persisted["classification"] == "partial"
    assert persisted["cited_file"] == "a.py:1"
    evaluator.flush(timeout=2.0)


def test_classify_passes_ground_truth_context_reconstructed_from_grounding_chunk_ids(
    session_store, vector_store, collection
):
    _seed_answered_question(session_store)
    llm_client = _FakeLLMClient()
    evaluator = Evaluator(session_store, vector_store, llm_client)
    evaluator.bind_session("sess1", collection)

    evaluator.classify("q1", "It works by doing Y.")

    question, ground_truth_context, user_answer = llm_client.classify_calls[0]
    assert question == "How does X work?"
    assert user_answer == "It works by doing Y."
    assert "def foo(): ..." in ground_truth_context
    assert "a.py" in ground_truth_context
    evaluator.flush(timeout=2.0)


def test_classify_with_no_grounding_chunks_passes_empty_context(session_store, vector_store, collection):
    _seed_answered_question(session_store, grounding=())
    llm_client = _FakeLLMClient()
    evaluator = Evaluator(session_store, vector_store, llm_client)
    evaluator.bind_session("sess1", collection)

    evaluator.classify("q1", "It works by doing Y.")

    assert llm_client.classify_calls[0][1] == ""
    evaluator.flush(timeout=2.0)


# -- background feedback -------------------------------------------------------

def test_background_worker_eventually_completes_feedback(session_store, vector_store, collection):
    _seed_answered_question(session_store)
    llm_client = _FakeLLMClient()
    evaluator = Evaluator(session_store, vector_store, llm_client)
    evaluator.bind_session("sess1", collection)

    evaluator.classify("q1", "It works by doing Y.")

    assert _wait_until(
        lambda: session_store.get_qa_record("sess1", "q1").eval_status in ("complete", "needs_review")
    )
    record = session_store.get_qa_record("sess1", "q1")
    assert record.eval_status == "complete"
    persisted = json.loads(record.eval_json)
    assert persisted["did_well"] == ["Explained the happy path."]
    assert persisted["improvement"] == "Look at retry handling."
    evaluator.flush(timeout=2.0)


def test_generate_feedback_receives_the_classification_just_persisted(session_store, vector_store, collection):
    _seed_answered_question(session_store)
    llm_client = _FakeLLMClient()
    evaluator = Evaluator(session_store, vector_store, llm_client)
    evaluator.bind_session("sess1", collection)

    evaluator.classify("q1", "It works by doing Y.")
    _wait_until(lambda: len(llm_client.feedback_calls) == 1)

    _, _, user_answer, classification = llm_client.feedback_calls[0]
    assert user_answer == "It works by doing Y."
    assert classification.classification == "partial"
    evaluator.flush(timeout=2.0)


def test_flush_waits_for_in_flight_work_within_timeout(session_store, vector_store, collection):
    _seed_answered_question(session_store)
    llm_client = _FakeLLMClient()
    llm_client.feedback_block_event = threading.Event()
    evaluator = Evaluator(session_store, vector_store, llm_client)
    evaluator.bind_session("sess1", collection)
    evaluator.classify("q1", "It works by doing Y.")

    def release_soon():
        time.sleep(0.1)
        llm_client.feedback_block_event.set()

    threading.Thread(target=release_soon).start()
    evaluator.flush(timeout=2.0)

    assert session_store.get_qa_record("sess1", "q1").eval_status == "complete"


def test_flush_timeout_marks_unfinished_work_needs_review_without_losing_classification(
    session_store, vector_store, collection
):
    _seed_answered_question(session_store)
    llm_client = _FakeLLMClient()
    llm_client.feedback_block_event = threading.Event()  # never set -- simulates a stuck call
    evaluator = Evaluator(session_store, vector_store, llm_client)
    evaluator.bind_session("sess1", collection)
    evaluator.classify("q1", "It works by doing Y.")

    evaluator.flush(timeout=0.2)

    record = session_store.get_qa_record("sess1", "q1")
    assert record.eval_status == "needs_review"
    # Classification-only eval_json survives -- degraded, not lost (NFR3).
    persisted = json.loads(record.eval_json)
    assert persisted["classification"] == "partial"

    # Unblock the stray worker thread and wait for it to actually finish
    # (not just wake up) before the test returns. Without this, the
    # worker can still be mid-write when the session_store fixture's
    # close() runs during teardown -- a real crash, not a flaky
    # assertion, if close() and a write race on the same connection
    # (see the close() fix in SessionStore). flush() again is the
    # public way to wait for the queue to fully drain.
    llm_client.feedback_block_event.set()
    evaluator.flush(timeout=2.0)


# -- resume ---------------------------------------------------------------------

def test_requeue_unfinished_before_bind_session_raises(session_store, vector_store):
    evaluator = Evaluator(session_store, vector_store, _FakeLLMClient())

    with pytest.raises(RuntimeError, match="bind_session"):
        evaluator.requeue_unfinished()


def test_requeue_unfinished_reprocesses_a_classified_but_not_yet_fed_back_record(
    session_store, vector_store, collection
):
    """Simulates a crash between classify()'s durable write and the
    worker thread ever running (§12.6): the record is 'classified' with
    no live Evaluator/worker behind it. A fresh Evaluator on resume must
    pick it back up."""
    _seed_answered_question(session_store)
    session_store.set_eval_classified(
        "sess1", "q1", json.dumps({"classification": "partial", "summary": "s", "cited_file": "a.py:1"})
    )

    llm_client = _FakeLLMClient()
    evaluator = Evaluator(session_store, vector_store, llm_client)
    evaluator.bind_session("sess1", collection)
    evaluator.requeue_unfinished()

    assert _wait_until(lambda: session_store.get_qa_record("sess1", "q1").eval_status == "complete")
    evaluator.flush(timeout=2.0)


def test_requeue_unfinished_ignores_terminal_records(session_store, vector_store, collection):
    _seed_answered_question(session_store)
    session_store.set_eval_complete(
        "sess1", "q1", json.dumps({"classification": "correct", "summary": "s"}), needs_review=False,
    )
    llm_client = _FakeLLMClient()
    evaluator = Evaluator(session_store, vector_store, llm_client)
    evaluator.bind_session("sess1", collection)

    evaluator.requeue_unfinished()
    evaluator.flush(timeout=2.0)

    assert llm_client.feedback_calls == []
