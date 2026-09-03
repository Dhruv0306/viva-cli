"""NFR7 retention enforcement (docs/plan.md Phase 9,
docs/system-design/14-phase-9-polish-design.md).

`run_cleanup` is a pure orchestration function over a real `SessionStore`
and `VectorStore` -- same dependency-injected, no-cross-component-import
shape `ReportBuilder` (`report.py`) established in Phase 8, and for the
same reason: nothing here is session-time or state-machine-relevant, so
it doesn't belong in `Orchestrator`. The `viva cleanup` CLI command
drives this directly, the same way `viva list` reads `SessionStore`
directly without going through the Orchestrator.

Collections are reference-counted against the `sessions` table itself
(§14.3) rather than a new bookkeeping structure: a `collection_name` is
only actually deleted from Chroma once no *surviving* session (one that
isn't being removed by this sweep) still points at it -- two sessions
against the same unchanged commit legitimately share one collection
(docs/system-design/05-repo-lifecycle-and-language-coverage.md §5.2),
and deleting it out from under a survivor would silently break that
session's `viva report`.

Retention age is judged by `updated_at`, not `created_at`: `viva resume`
bumps `updated_at` on every state transition
(`SessionStore.update_status`), so an old session someone is actively
resuming is never swept just because it was first created outside the
window.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from viva.indexer.store import VectorStore
from viva.storage.session_store import SessionRecord, SessionStore


@dataclass(frozen=True)
class CleanupReport:
    sessions_removed: list[str]
    collections_removed: list[str]
    profiles_removed: list[str]
    sessions_retained: int

    @property
    def is_empty(self) -> bool:
        return not (self.sessions_removed or self.collections_removed or self.profiles_removed)


def _cutoff_iso(older_than_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()


def run_cleanup(
    store: SessionStore,
    vector_store: VectorStore,
    older_than_days: int,
    purge_all: bool = False,
) -> CleanupReport:
    """Removes session/Q&A records, Project Profile JSON files, and
    Chroma collections past retention (or everything, if `purge_all`).

    `older_than_days` is ignored when `purge_all` is set -- callers
    still pass a value (the CLI command always has one, defaulting to
    `Config.session_retention_days`), but every session becomes a
    target regardless of age.
    """
    all_sessions = store.list_sessions()
    cutoff = None if purge_all else _cutoff_iso(older_than_days)

    def _is_target(session: SessionRecord) -> bool:
        return purge_all or session.updated_at < cutoff

    targets = [s for s in all_sessions if _is_target(s)]
    survivors = [s for s in all_sessions if not _is_target(s)]
    survivor_collections = {s.collection_name for s in survivors if s.collection_name}

    sessions_removed: list[str] = []
    profiles_removed: list[str] = []
    collections_touched: set[str] = set()

    for session in targets:
        if session.profile_path and os.path.exists(session.profile_path):
            os.remove(session.profile_path)
            profiles_removed.append(session.profile_path)
        if session.collection_name:
            collections_touched.add(session.collection_name)
        store.delete_session(session.session_id)
        sessions_removed.append(session.session_id)

    collections_removed: list[str] = []
    for name in sorted(collections_touched - survivor_collections):
        if vector_store.collection_exists(name):
            vector_store.delete_collection(name)
            collections_removed.append(name)

    return CleanupReport(
        sessions_removed=sessions_removed,
        collections_removed=collections_removed,
        profiles_removed=profiles_removed,
        sessions_retained=len(survivors),
    )
