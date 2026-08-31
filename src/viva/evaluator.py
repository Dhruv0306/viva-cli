"""The Evaluator (docs/plan.md Phase 7, docs/system-design/
12-phase-7-evaluator-design.md).

The real `ClassificationProvider` implementation -- swaps in for
`NullClassificationProvider` (`viva.classification`) with no change to the
Orchestrator's control flow around FR14 (`_maybe_queue_followup` still
just calls `classification_provider.classify(question_id, answer_text)`).

Owns all three Phase 7 pieces: the two evaluation calls
(`LLMClient.classify_answer`/`generate_feedback`), ground-truth
reconstruction via `VectorStore.get_by_ids` (§12.3), and `eval_status`/
`eval_json` persistence through `SessionStore` (§12.5).

Backgrounding model (§12.4): one long-lived daemon worker thread plus a
`queue.Queue`, started per session via `bind_session()` -- not a thread
per answer. This mirrors `RichSessionUI`'s single background thread and
avoids concurrent SQLite writers, which `SessionStore` isn't designed
for. `classify()` runs call #1 inline (fast, drives FR14 synchronously,
persisted durably before returning) and enqueues call #2; the worker
thread drains the queue one job at a time.
"""
from __future__ import annotations

import json
import logging
import queue
import threading

from viva.classification import ClassificationProvider
from viva.indexer.store import VectorStore
from viva.llm_client import LLMClient
from viva.schemas import Classification, ClassificationResult, EvaluationRecord
from viva.storage import SessionStore

logger = logging.getLogger(__name__)

# Sentinel pushed onto the queue by flush() to signal "no more work is
# coming, stop after draining what's already queued" -- an object(), not a
# string, so it can never collide with a real question_id.
_SENTINEL = object()

# Same join convention as questiongen/__init__.generate_question, so both
# evaluation calls see grounding text in the same shape the question was
# actually generated from.
_GROUNDING_JOIN = "\n\n---\n\n"


def _build_ground_truth_context(chunks: list[dict]) -> str:
    return _GROUNDING_JOIN.join(
        f"[{c['metadata']['filepath']}:{c['metadata']['start_line']}-{c['metadata']['end_line']}]\n{c['text']}"
        for c in chunks
    )


def _classification_from_eval_json(eval_json: str | None) -> ClassificationResult | None:
    """Reconstructs call #1's verdict from the classification-only
    `eval_json` `classify()` already persisted -- `generate_feedback`
    needs it as context (§12.2), and the worker thread runs in a
    separate call from `classify()`, so it re-reads rather than holding
    call #1's result in memory across the queue boundary."""
    if not eval_json:
        return None
    try:
        data = json.loads(eval_json)
    except json.JSONDecodeError:
        return None
    try:
        return ClassificationResult(
            classification=data.get("classification", "not_attempted"),
            summary=data.get("summary") or "(no summary)",
            cited_file=data.get("cited_file"),
            needs_review=data.get("needs_review", False),
        )
    except Exception:  # noqa: BLE001 - malformed persisted JSON must not crash the worker
        return None


class Evaluator(ClassificationProvider):
    def __init__(
        self,
        session_store: SessionStore,
        vector_store: VectorStore,
        llm_client: LLMClient,
    ) -> None:
        self._store = session_store
        self._vector_store = vector_store
        self._llm_client = llm_client
        self._session_id: str | None = None
        self._collection_name: str | None = None
        self._queue: "queue.Queue[object]" = queue.Queue()
        self._worker: threading.Thread | None = None

    def bind_session(self, session_id: str, collection_name: str) -> None:
        """Must be called once, before the first `classify()`, right
        where the Orchestrator enters `IN_PROGRESS` -- starts the
        background worker thread for this session. A fresh Orchestrator
        (and Evaluator) is constructed per CLI invocation (`start()`/
        `resume()`), so this only ever runs once per instance."""
        self._session_id = session_id
        self._collection_name = collection_name
        self._worker = threading.Thread(
            target=self._run_worker, daemon=True, name="viva-evaluator"
        )
        self._worker.start()

    # -- ClassificationProvider ----------------------------------------------

    def classify(self, question_id: str, answer_text: str) -> Classification | None:
        if self._session_id is None:
            raise RuntimeError("Evaluator.bind_session() must be called before classify()")

        record = self._store.get_qa_record(self._session_id, question_id)
        if record is None or not record.question_text:
            return None

        ground_truth_context = self._ground_truth_context(record.grounding_chunk_ids)
        call_result = self._llm_client.classify_answer(
            question=record.question_text,
            ground_truth_context=ground_truth_context,
            user_answer=answer_text,
        )
        classification = call_result.result

        # Durable before any backgrounding happens (NFR3): if the process
        # dies before the feedback call even starts, this classification
        # survives as the eval_json fallback.
        partial_record = EvaluationRecord(
            classification=classification.classification,
            summary=classification.summary,
            cited_file=classification.cited_file,
            improvement="",
            needs_review=classification.needs_review,
        )
        self._store.set_eval_classified(
            self._session_id, question_id, partial_record.model_dump_json()
        )
        self._enqueue(question_id)
        return classification.classification

    # -- background worker ---------------------------------------------------

    def _enqueue(self, question_id: str) -> None:
        self._store.set_eval_feedback_pending(self._session_id, question_id)
        self._queue.put(question_id)

    def _run_worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _SENTINEL:
                    return
                self._run_feedback(item)
            except Exception:  # noqa: BLE001 - one bad job must never kill the worker
                logger.exception("Evaluator worker failed processing %r", item)
                if item is not _SENTINEL:
                    self._store.mark_eval_needs_review(self._session_id, item)
            finally:
                self._queue.task_done()

    def _run_feedback(self, question_id: str) -> None:
        record = self._store.get_qa_record(self._session_id, question_id)
        if record is None or not record.question_text:
            return
        classification = _classification_from_eval_json(record.eval_json)
        if classification is None:
            return

        ground_truth_context = self._ground_truth_context(record.grounding_chunk_ids)
        call_result = self._llm_client.generate_feedback(
            question=record.question_text,
            ground_truth_context=ground_truth_context,
            user_answer=record.answer_text or "",
            classification=classification,
        )
        feedback = call_result.result
        merged = EvaluationRecord.from_calls(classification, feedback)
        self._store.set_eval_complete(
            self._session_id, question_id, merged.model_dump_json(),
            needs_review=merged.needs_review,
        )

    def _ground_truth_context(self, grounding_chunk_ids: list[str]) -> str:
        if not grounding_chunk_ids or self._collection_name is None:
            return ""
        chunks = self._vector_store.get_by_ids(self._collection_name, grounding_chunk_ids)
        return _build_ground_truth_context(chunks)

    # -- resume / finalize ----------------------------------------------------

    def requeue_unfinished(self) -> None:
        """`viva resume`: re-enqueue anything a crashed prior process
        left mid-evaluation (§12.6). Call after `bind_session()`, before
        the live loop resumes -- `grounding_chunk_ids` plus the persisted
        question/answer text is sufficient to fully regenerate feedback,
        so this is silent, not a user-facing message."""
        if self._session_id is None:
            raise RuntimeError("Evaluator.bind_session() must be called before requeue_unfinished()")
        for record in self._store.get_records_needing_feedback(self._session_id):
            self._store.set_eval_feedback_pending(self._session_id, record.question_id)
            self._queue.put(record.question_id)

    def flush(self, timeout: float) -> None:
        """`FINALIZING_EVALS`: drain the queue, bounded by `timeout` so
        session end can't hang indefinitely on a stuck model call. Any
        record still unfinished when the timeout hits is marked
        `needs_review` -- degraded (classification-only, no feedback
        text) but never lost (NFR3)."""
        if self._worker is None:
            return
        self._queue.put(_SENTINEL)
        self._worker.join(timeout=timeout)
        if self._worker.is_alive():
            # The worker is stuck on whatever job it's currently
            # processing -- everything still queued behind it (including
            # the sentinel) never got a chance to run.
            for record in self._store.get_records_needing_feedback(self._session_id):
                self._store.mark_eval_needs_review(self._session_id, record.question_id)
