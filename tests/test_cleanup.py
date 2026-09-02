"""Tests for viva.cleanup (docs/plan.md Phase 9, NFR7,
docs/system-design/14-phase-9-polish-design.md).

Exercises real SessionStore/VectorStore against tmp_path, same
"test real behavior where it's cheap" approach test_indexer_store.py
and test_cli_report.py already use -- nothing here is worth mocking.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from viva.cleanup import run_cleanup
from viva.indexer.models import Chunk
from viva.indexer.store import VectorStore
from viva.storage.session_store import SessionStore


@pytest.fixture
def store(tmp_path):
    s = SessionStore(str(tmp_path / "viva.db"))
    yield s
    s.close()


@pytest.fixture
def vector_store(tmp_path):
    return VectorStore(str(tmp_path / "chroma"))


def _chunk(id: str) -> Chunk:
    return Chunk(
        id=id, text="def foo(): ...", filepath="src/app/main.py", module="src",
        symbol_name="foo", kind="function", parse_method="ast", language="python",
        start_line=1, end_line=3,
    )


def _age_session(store: SessionStore, session_id: str, days_old: int) -> None:
    """Test-only helper: backdates a session's updated_at directly via
    SQL, since SessionStore has no public "set updated_at" -- every real
    write path stamps `now()` itself (schema.py's `sessions.updated_at`,
    stamped by every mutating SessionStore method). Aging a session for
    a retention test needs to bypass that, same as any test that needs
    to simulate "time has passed" without an injectable clock.
    """
    from datetime import datetime, timedelta, timezone

    backdated = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    with store._lock:  # noqa: SLF001 - test-only direct access, see docstring
        store._conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (backdated, session_id),
        )
        store._conn.commit()


def _seed_session(
    store: SessionStore,
    session_id: str,
    profile_path: str | None = None,
    collection_name: str | None = None,
) -> None:
    store.create_session(session_id, "https://github.com/o/r", None, None, 1800)
    if profile_path or collection_name:
        store.set_pipeline_artifacts(
            session_id, repo_slug="o/r", commit_sha="abc123def456",
            collection_name=collection_name or "", profile_path=profile_path or "",
        )


def test_no_sessions_is_a_noop(store, vector_store):
    report = run_cleanup(store, vector_store, older_than_days=7)

    assert report.is_empty
    assert report.sessions_retained == 0


def test_recent_session_is_retained(store, vector_store):
    _seed_session(store, "sess1")

    report = run_cleanup(store, vector_store, older_than_days=7)

    assert report.sessions_removed == []
    assert report.sessions_retained == 1
    assert store.get_session("sess1") is not None


def test_old_session_is_removed(store, vector_store):
    _seed_session(store, "sess1")
    _age_session(store, "sess1", days_old=10)

    report = run_cleanup(store, vector_store, older_than_days=7)

    assert report.sessions_removed == ["sess1"]
    assert report.sessions_retained == 0
    assert store.get_session("sess1") is None


def test_old_session_profile_file_is_removed(store, vector_store, tmp_path):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({"repo": "o/r"}))
    _seed_session(store, "sess1", profile_path=str(profile_path))
    _age_session(store, "sess1", days_old=10)

    report = run_cleanup(store, vector_store, older_than_days=7)

    assert report.profiles_removed == [str(profile_path)]
    assert not profile_path.exists()


def test_missing_profile_file_is_not_an_error(store, vector_store, tmp_path):
    profile_path = tmp_path / "already-gone.json"
    _seed_session(store, "sess1", profile_path=str(profile_path))
    _age_session(store, "sess1", days_old=10)

    report = run_cleanup(store, vector_store, older_than_days=7)

    assert report.sessions_removed == ["sess1"]
    assert report.profiles_removed == []


def test_unshared_collection_is_removed_with_its_session(store, vector_store):
    vector_store.upsert_chunks("o--r-abc123def456", [_chunk("c1")], [[0.1, 0.2, 0.3]])
    _seed_session(store, "sess1", collection_name="o--r-abc123def456")
    _age_session(store, "sess1", days_old=10)

    report = run_cleanup(store, vector_store, older_than_days=7)

    assert report.collections_removed == ["o--r-abc123def456"]
    assert vector_store.collection_exists("o--r-abc123def456") is False


def test_shared_collection_survives_if_another_session_still_uses_it(store, vector_store):
    # sess1 and sess2 ran against the same unchanged commit and reuse
    # the same collection (05-repo-lifecycle...md §5.2). sess1 ages out;
    # sess2 doesn't -- the collection must not be deleted out from under
    # sess2's still-valid `viva report`.
    vector_store.upsert_chunks("o--r-abc123def456", [_chunk("c1")], [[0.1, 0.2, 0.3]])
    _seed_session(store, "sess1", collection_name="o--r-abc123def456")
    _seed_session(store, "sess2", collection_name="o--r-abc123def456")
    _age_session(store, "sess1", days_old=10)

    report = run_cleanup(store, vector_store, older_than_days=7)

    assert report.sessions_removed == ["sess1"]
    assert report.collections_removed == []
    assert vector_store.collection_exists("o--r-abc123def456") is True
    assert store.get_session("sess2") is not None


def test_purge_all_removes_every_session_regardless_of_age(store, vector_store):
    vector_store.upsert_chunks("o--r-abc123def456", [_chunk("c1")], [[0.1, 0.2, 0.3]])
    _seed_session(store, "sess1", collection_name="o--r-abc123def456")
    _seed_session(store, "sess2", collection_name="o--r-abc123def456")
    # Neither session is aged -- both are "recent" by updated_at.

    report = run_cleanup(store, vector_store, older_than_days=7, purge_all=True)

    assert sorted(report.sessions_removed) == ["sess1", "sess2"]
    assert report.collections_removed == ["o--r-abc123def456"]
    assert report.sessions_retained == 0


def test_session_with_no_collection_or_profile_is_removed_cleanly(store, vector_store):
    _seed_session(store, "sess1")
    _age_session(store, "sess1", days_old=10)

    report = run_cleanup(store, vector_store, older_than_days=7)

    assert report.sessions_removed == ["sess1"]
    assert report.collections_removed == []
    assert report.profiles_removed == []
