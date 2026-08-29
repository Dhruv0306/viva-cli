"""`SessionStore`: the Orchestrator's single interface to session
persistence (docs/plan.md Phase 6, docs/design.md §8).

Every write goes through here, and callers never see raw SQL or
`sqlite3.Row` objects -- they get `SessionRecord`/`QARecordRow`, mirroring
how `indexer/store.py`'s `VectorStore` hides Chroma behind plain
dataclasses/dicts.
"""
from __future__ import annotations

import json
import sqlite3
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

    def close(self) -> None:
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
        self._conn.execute(
            "INSERT INTO sessions (session_id, repo_url, repo_slug, commit_sha, "
            "branch, session_name, status, duration_seconds, collection_name, "
            "profile_path, error_message, created_at, updated_at) "
            "VALUES (?, ?, NULL, NULL, ?, ?, 'INGESTING', ?, NULL, NULL, NULL, ?, ?)",
            (session_id, repo_url, branch, session_name, duration_seconds, now, now),
        )
        self._conn.commit()

    def update_status(self, session_id: str, status: str) -> None:
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
        self._conn.execute(
            "UPDATE sessions SET repo_slug = ?, commit_sha = ?, collection_name = ?, "
            "profile_path = ?, updated_at = ? WHERE session_id = ?",
            (repo_slug, commit_sha, collection_name, profile_path, _now_iso(), session_id),
        )
        self._conn.commit()

    def set_failed(self, session_id: str, message: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET status = 'FAILED', error_message = ?, updated_at = ? "
            "WHERE session_id = ?",
            (message, _now_iso(), session_id),
        )
        self._conn.commit()

    def get_session(self, session_id: str) -> SessionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return _session_from_row(row) if row else None

    def list_sessions(self, status: str | None = None) -> list[SessionRecord]:
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
        self._conn.execute(
            "UPDATE qa_records SET question_text = ?, grounding_chunk_ids_json = ?, "
            "status = ?, asked_at = ? WHERE session_id = ? AND question_id = ?",
            (question_text, json.dumps(grounding_chunk_ids), ASKED, _now_iso(), session_id, question_id),
        )
        self._conn.commit()

    def record_answer(self, session_id: str, question_id: str, answer_text: str) -> None:
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
        self._conn.execute(
            "UPDATE qa_records SET status = ? WHERE session_id = ? AND question_id = ?",
            (status, session_id, question_id),
        )
        self._conn.commit()

    def get_pending_plan_items(self, session_id: str) -> list[QARecordRow]:
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
        cursor = self._conn.execute(
            "UPDATE qa_records SET status = ? WHERE session_id = ? AND status = ?",
            (PENDING, session_id, ASKED),
        )
        self._conn.commit()
        return cursor.rowcount

    def get_qa_records(self, session_id: str) -> list[QARecordRow]:
        rows = self._conn.execute(
            "SELECT * FROM qa_records WHERE session_id = ? ORDER BY rowid",
            (session_id,),
        ).fetchall()
        return [_qa_from_row(row) for row in rows]
