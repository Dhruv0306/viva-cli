"""SQLite schema for session persistence (docs/plan.md Phase 6,
docs/design.md §8).

Two tables map directly onto docs/design.md §6's Project Profile / Q&A
Record contracts and the session-level fields the CLI contract
(docs/system-design/06-cli-contract-and-profile-scaling.md §6.1) needs
for `viva list`/`viva resume`. Plain `sqlite3`, no ORM -- the schema is
small and stable enough that an ORM would add a dependency for no real
benefit, consistent with the project's existing convention of thin
direct-client wrappers (Chroma via `indexer/store.py`, Ollama via
`llm_client.py`) rather than a heavier abstraction layer.

`status` on `sessions` holds the design.md §2 state-machine values
(`INGESTING`, `ANALYZING`, `INDEXING`, `PLANNING`, `IN_PROGRESS`,
`TIME_EXPIRED`, `QUESTIONS_EXHAUSTED`, `FINALIZING_EVALS`, `SUMMARIZING`,
`COMPLETE`), plus one pragmatic addition not in that state diagram:
`FAILED`, for a session that errored out before reaching a real
terminal state (e.g. clone failure during `INGESTING`) -- see
docs/system-design/11-phase-6-session-loop-design.md for why this was
added rather than silently leaving such sessions stuck. Not enforced as
a SQL CHECK constraint (kept a plain TEXT column) since Phase 7/8 will
likely add states of their own; validity is enforced at the Python layer
instead.

`qa_records.status` tracks each plan item's lifecycle: `pending` ->
`asked` -> `answered`, or `skipped_no_grounding` /
`skipped_time_collapse` if it's dropped before being asked (see
`orchestrator.py`). `eval_status` is separate from `status` -- it's
always `'deferred'` in Phase 6 (no Evaluator exists yet, see
`viva.classification`), populated for real in Phase 7.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    repo_url TEXT NOT NULL,
    repo_slug TEXT,
    commit_sha TEXT,
    branch TEXT,
    session_name TEXT,
    status TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    collection_name TEXT,
    profile_path TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qa_records (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    question_id TEXT NOT NULL,
    category TEXT NOT NULL,
    target_module TEXT,
    target_file TEXT,
    is_followup_of TEXT,
    question_text TEXT,
    grounding_chunk_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    answer_text TEXT,
    asked_at TEXT,
    answered_at TEXT,
    eval_status TEXT NOT NULL DEFAULT 'deferred',
    eval_json TEXT,
    PRIMARY KEY (session_id, question_id)
);

-- No explicit ordering column: insertion (plan) order is recovered via
-- SQLite's implicit rowid, which is monotonic for INSERTs on a table
-- with a non-INTEGER PRIMARY KEY like this one.
CREATE INDEX IF NOT EXISTS idx_qa_records_session ON qa_records(session_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection to the session database, creating parent
    directories and the schema if they don't exist yet.

    One writer (the Orchestrator, design.md §8) -- callers should not
    fan this connection out across threads/processes.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()
