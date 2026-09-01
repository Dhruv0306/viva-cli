"""`SessionStore`: the Orchestrator's single interface to session
persistence (docs/plan.md Phase 6, docs/design.md §8).

Every write goes through here, and callers never see raw SQL or
`sqlite3.Row` objects -- they get `SessionRecord`/`QARecordRow`, mirroring
how `indexer/store.py`'s `VectorStore` hides Chroma behind plain
dataclasses/dicts.

As of Phase 7 (docs/system-design/12-phase-7-evaluator-design.md §12.4),
this is accessed from two threads -- the Orchestrator's main thread and
the Evaluator's background worker thread -- so every method acquires
`self._lock` around its `self._conn` access. This isn't fine-grained
concurrency (all access is fully serialized), but writes here are never
the bottleneck compared to an LLM call, so a single lock is the simplest
correct thing rather than a premature optimization.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from viva.storage import schema

# Terminal/non-terminal statuses a qa_records row can hold. Not enforced
# as a SQL CHECK (see schema.py's docstring) -- validated here instead.
PENDING = "pending"
ASKED = "asked"
ANSWERED = "answered"
SKIPPED_NO_GROUNDING = "skipped_no_grounding"
SKIPPED_TIME_COLLAPSE = "skipped_time_collapse"
SKIPPED_DUPLICATE_TARGET = "skipped_duplicate_target"

# eval_status states (docs/system-design/12-phase-7-evaluator-design.md
# §12.5): deferred (Phase 6 default, before any evaluation call runs) ->
# classified (call #1 done, persisted durably before backgrounding call
# #2) -> feedback_pending (enqueued to the Evaluator's worker thread) ->
# terminal complete/needs_review.
EVAL_DEFERRED = "deferred"
EVAL_CLASSIFIED = "classified"
EVAL_FEEDBACK_PENDING = "feedback_pending"
EVAL_COMPLETE = "complete"
EVAL_NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    repo_url: str
    repo_slug: str | None
    commit_sha: str | None
    branch: str | None
    session_name: str | None
    status: str
    duration_seconds: float
    collection_name: str | None
    profile_path: str | None
    error_message: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class QARecordRow:
    session_id: str
    question_id: str
    category: str
    target_module: str | None
    target_file: str | None
    is_followup_of: str | None
    question_text: str | None
    grounding_chunk_ids: list[str]
    status: str
    answer_text: str | None
    asked_at: str | None
    answered_at: str | None
    eval_status: str
    eval_json: str | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_from_row(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        session_id=row["session_id"],
        repo_url=row["repo_url"],
        repo_slug=row["repo_slug"],
        commit_sha=row["commit_sha"],
        branch=row["branch"],
        session_name=row["session_name"],
        status=row["status"],
        duration_seconds=row["duration_seconds"],
        collection_name=row["collection_name"],
        profile_path=row["profile_path"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _qa_from_row(row: sqlite3.Row) -> QARecordRow:
    return QARecordRow(
        session_id=row["session_id"],
        question_id=row["question_id"],
        category=row["category"],
        target_module=row["target_module"],
        target_file=row["target_file"],
        is_followup_of=row["is_followup_of"],
        question_text=row["question_text"],
        grounding_chunk_ids=json.loads(row["grounding_chunk_ids_json"]),
        status=row["status"],
        answer_text=row["answer_text"],
        asked_at=row["asked_at"],
        answered_at=row["answered_at"],
        eval_status=row["eval_status"],
        eval_json=row["eval_json"],
    )


class SessionStore:
    def __init__(self, db_path: str) -> None:
        self._conn = schema.connect(db_path)
        self._lock = threading.Lock()

    def close(self) -> None:
        # Must hold the same lock every write method does: closing the
        # connection while another thread (the Evaluator's worker) is
        # mid-execute()/commit() on it is a real crash, not just a
        # Python exception -- sqlite3's C extension segfaulted in CI
        # when this raced set_eval_complete() (docs/system-design/
        # 12-phase-7-evaluator-design.md §12.4's worker thread). Every
        # other method already serializes through self._lock; close()
        # was the one gap.
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "SessionStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- session lifecycle -------------------------------------------------

    def create_session(
        self,
        session_id: str,
        repo_url: str,
        branch: str | None,
        session_name: str | None,
        duration_seconds: float,
    ) -> None:
        """Insert the session row at status `INGESTING` -- called right
        before cloning starts, so the CLI can print `session_id` to
        stdout immediately per the CLI contract §6.1, well before
        `repo_slug`/`commit_sha` are known.
        """
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (session_id, repo_url, repo_slug, commit_sha, "
                "branch, session_name, status, duration_seconds, collection_name, "
                "profile_path, error_message, created_at, updated_at) "
                "VALUES (?, ?, NULL, NULL, ?, ?, 'INGESTING', ?, NULL, NULL, NULL, ?, ?)",
                (session_id, repo_url, branch, session_name, duration_seconds, now, now),
            )
            self._conn.commit()

    def update_status(self, session_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                (status, _now_iso(), session_id),
            )
            self._conn.commit()

    def set_pipeline_artifacts(
        self,
        session_id: str,
        repo_slug: str,
        commit_sha: str,
        collection_name: str,
        profile_path: str,
    ) -> None:
        """Persist what `INGESTING`/`INDEXING` resolved, once known --
        `repo_slug`/`commit_sha` weren't available at `create_session()`
        time (see its docstring).
        """
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET repo_slug = ?, commit_sha = ?, collection_name = ?, "
                "profile_path = ?, updated_at = ? WHERE session_id = ?",
                (repo_slug, commit_sha, collection_name, profile_path, _now_iso(), session_id),
            )
            self._conn.commit()

    def set_failed(self, session_id: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status = 'FAILED', error_message = ?, updated_at = ? "
                "WHERE session_id = ?",
                (message, _now_iso(), session_id),
            )
            self._conn.commit()

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return _session_from_row(row) if row else None

    def list_sessions(self, status: str | None = None) -> list[SessionRecord]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM sessions WHERE status = ? ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM sessions ORDER BY created_at DESC"
                ).fetchall()
        return [_session_from_row(row) for row in rows]

    # -- plan / Q&A records --------------------------------------------------

    def save_plan(self, session_id: str, plan_items: list) -> None:
        """Insert one `pending` qa_records row per `QuestionPlanItem`
        (`questiongen/models.py`). `INSERT OR IGNORE` makes this safe to
        call again for a follow-up item without risking a duplicate-key
        error on an already-planned `question_id`.
        """
        rows = [
            (
                session_id,
                item.id,
                item.category,
                item.target_module,
                item.target_file,
                item.is_followup_of,
            )
            for item in plan_items
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO qa_records "
                "(session_id, question_id, category, target_module, target_file, is_followup_of) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def add_followup_item(self, session_id: str, plan_item) -> None:
        """FR14 seam: inserts one follow-up plan item. Never actually
        called in Phase 6 (see `viva.classification`), but the mechanism
        is real and tested so Phase 7 can call it with no changes here.
        """
        self.save_plan(session_id, [plan_item])

    def record_question_asked(
        self,
        session_id: str,
        question_id: str,
        question_text: str,
        grounding_chunk_ids: list[str],
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE qa_records SET question_text = ?, grounding_chunk_ids_json = ?, "
                "status = ?, asked_at = ? WHERE session_id = ? AND question_id = ?",
                (question_text, json.dumps(grounding_chunk_ids), ASKED, _now_iso(), session_id, question_id),
            )
            self._conn.commit()

    def record_answer(self, session_id: str, question_id: str, answer_text: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE qa_records SET answer_text = ?, status = ?, answered_at = ?, "
                "eval_status = 'deferred' WHERE session_id = ? AND question_id = ?",
                (answer_text, ANSWERED, _now_iso(), session_id, question_id),
            )
            self._conn.commit()

    def mark_item_status(self, session_id: str, question_id: str, status: str) -> None:
        """Generic status setter for the non-answer terminal states a
        plan item can land in without ever being asked
        (`skipped_no_grounding`, `skipped_time_collapse`)."""
        with self._lock:
            self._conn.execute(
                "UPDATE qa_records SET status = ? WHERE session_id = ? AND question_id = ?",
                (status, session_id, question_id),
            )
            self._conn.commit()

    def get_pending_plan_items(self, session_id: str) -> list[QARecordRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM qa_records WHERE session_id = ? AND status = ? ORDER BY rowid",
                (session_id, PENDING),
            ).fetchall()
        return [_qa_from_row(row) for row in rows]

    def requeue_orphaned_asked_items(self, session_id: str) -> int:
        """A session can be interrupted between `record_question_asked()`
        and `record_answer()` -- the process dies while a question is on
        screen and the person is mid-answer. That item is stuck at
        `asked` forever unless something notices on resume: `pending`
        only covers items never presented at all. Resets any such item
        back to `pending` (its `question_text`/`grounding_chunk_ids` stay
        intact, so the Orchestrator can re-present it without paying for
        another LLM generation call) and returns how many were requeued.
        """
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE qa_records SET status = ? WHERE session_id = ? AND status = ?",
                (PENDING, session_id, ASKED),
            )
            self._conn.commit()
            return cursor.rowcount

    def get_qa_records(self, session_id: str) -> list[QARecordRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM qa_records WHERE session_id = ? ORDER BY rowid",
                (session_id,),
            ).fetchall()
        return [_qa_from_row(row) for row in rows]

    def get_qa_record(self, session_id: str, question_id: str) -> QARecordRow | None:
        """Single-row lookup -- the Evaluator's `classify()` (the
        `ClassificationProvider` seam, docs/system-design/
        12-phase-7-evaluator-design.md §12.2) only receives `question_id`/
        `answer_text`, so it needs this to recover `question_text`/
        `grounding_chunk_ids` for the LLM call."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM qa_records WHERE session_id = ? AND question_id = ?",
                (session_id, question_id),
            ).fetchone()
        return _qa_from_row(row) if row else None

    # -- evaluation persistence (Phase 7, docs/system-design/
    # 12-phase-7-evaluator-design.md §12.2/§12.5) -----------------------------

    def set_eval_classified(self, session_id: str, question_id: str, eval_json: str) -> None:
        """Call #1 done: persisted durably *before* the feedback call is
        even enqueued, so a crash before backgrounding starts never loses
        the classification (NFR3)."""
        with self._lock:
            self._conn.execute(
                "UPDATE qa_records SET eval_status = ?, eval_json = ? "
                "WHERE session_id = ? AND question_id = ?",
                (EVAL_CLASSIFIED, eval_json, session_id, question_id),
            )
            self._conn.commit()

    def set_eval_feedback_pending(self, session_id: str, question_id: str) -> None:
        """Marks a record as enqueued to the Evaluator's worker thread --
        distinguishes "call #1 done, call #2 not started" (`classified`)
        from "call #2 in flight" for `viva resume`'s requeue logic."""
        with self._lock:
            self._conn.execute(
                "UPDATE qa_records SET eval_status = ? WHERE session_id = ? AND question_id = ?",
                (EVAL_FEEDBACK_PENDING, session_id, question_id),
            )
            self._conn.commit()

    def set_eval_complete(
        self, session_id: str, question_id: str, eval_json: str, needs_review: bool
    ) -> None:
        """Call #2 done: the merged `EvaluationRecord` (classification +
        feedback) replaces the classification-only `eval_json` written by
        `set_eval_classified`."""
        status = EVAL_NEEDS_REVIEW if needs_review else EVAL_COMPLETE
        with self._lock:
            self._conn.execute(
                "UPDATE qa_records SET eval_status = ?, eval_json = ? "
                "WHERE session_id = ? AND question_id = ?",
                (status, eval_json, session_id, question_id),
            )
            self._conn.commit()

    def mark_eval_needs_review(self, session_id: str, question_id: str) -> None:
        """`FINALIZING_EVALS` flush-timeout path (docs/system-design/
        12-phase-7-evaluator-design.md §12.6): call #2 never finished in
        time, so the record stays at whatever `eval_json` call #1 already
        wrote (classification-only) -- degraded, never lost."""
        with self._lock:
            self._conn.execute(
                "UPDATE qa_records SET eval_status = ? WHERE session_id = ? AND question_id = ?",
                (EVAL_NEEDS_REVIEW, session_id, question_id),
            )
            self._conn.commit()

    def get_records_needing_feedback(self, session_id: str) -> list[QARecordRow]:
        """Records a prior process left mid-evaluation when it died --
        `eval_status` in (`classified`, `feedback_pending`) -- for
        `viva resume`'s re-enqueue (docs/system-design/
        12-phase-7-evaluator-design.md §12.6). `grounding_chunk_ids` plus
        the persisted question/answer text is sufficient to fully
        regenerate feedback; nothing here is time-sensitive."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM qa_records WHERE session_id = ? AND eval_status IN (?, ?) "
                "ORDER BY rowid",
                (session_id, EVAL_CLASSIFIED, EVAL_FEEDBACK_PENDING),
            ).fetchall()
        return [_qa_from_row(row) for row in rows]
