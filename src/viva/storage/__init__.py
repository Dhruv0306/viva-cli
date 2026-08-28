"""Session persistence (docs/plan.md Phase 6, docs/design.md §8).

SQLite, single writer (the Orchestrator). Per design.md's component rule
("no service calls another directly"), `SessionStore` is the seam the
Orchestrator uses -- nothing outside this package should import `schema`
or touch `sqlite3` directly.

Public entrypoint: `SessionStore`.
"""
from __future__ import annotations

from viva.storage.session_store import QARecordRow, SessionRecord, SessionStore

__all__ = ["SessionStore", "SessionRecord", "QARecordRow"]
