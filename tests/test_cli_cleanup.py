"""Tests for `viva cleanup` (docs/plan.md Phase 9, NFR7, CLI contract
§6.1, docs/system-design/14-phase-9-polish-design.md).

Exercises a real SessionStore/VectorStore against tmp-path-backed DBs,
following test_cli_report.py's pattern -- this command's whole job is
sweeping what's actually persisted, so there's nothing worth mocking.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from viva.cli import app
from viva.indexer.models import Chunk
from viva.indexer.store import VectorStore
from viva.storage import SessionStore

runner = CliRunner()


def _chunk(id: str) -> Chunk:
    return Chunk(
        id=id, text="def foo(): ...", filepath="src/app/main.py", module="src",
        symbol_name="foo", kind="function", parse_method="ast", language="python",
        start_line=1, end_line=3,
    )


def _seed_session(db_path: str, session_id: str, days_old: int = 0,
                   collection_name: str = "", profile_path: str = "") -> None:
    store = SessionStore(db_path)
    store.create_session(session_id, "https://github.com/o/r", None, None, 1800)
    if collection_name or profile_path:
        store.set_pipeline_artifacts(
            session_id, repo_slug="o/r", commit_sha="abc123def456",
            collection_name=collection_name, profile_path=profile_path,
        )
    if days_old:
        backdated = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
        with store._lock:  # noqa: SLF001 - test-only, no public "set updated_at"
            store._conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (backdated, session_id),
            )
            store._conn.commit()
    store.close()


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "viva.db"))
    monkeypatch.setenv("VECTOR_DB_PATH", str(tmp_path / "chroma"))


def test_cleanup_with_no_sessions_reports_nothing_to_clean_up(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["cleanup"])

    assert result.exit_code == 0
    assert "Nothing to clean up" in result.stdout


def test_cleanup_retains_recent_session(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _seed_session(str(tmp_path / "viva.db"), "sess1")

    result = runner.invoke(app, ["cleanup", "--older-than", "7"])

    assert result.exit_code == 0
    assert "Removed 0 session(s)" in result.stdout
    assert "1 session(s) retained." in result.stdout
    store = SessionStore(str(tmp_path / "viva.db"))
    assert store.get_session("sess1") is not None
    store.close()


def test_cleanup_removes_old_session(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _seed_session(str(tmp_path / "viva.db"), "sess1", days_old=10)

    result = runner.invoke(app, ["cleanup", "--older-than", "7"])

    assert result.exit_code == 0
    assert "Removed 1 session(s)" in result.stdout
    store = SessionStore(str(tmp_path / "viva.db"))
    assert store.get_session("sess1") is None
    store.close()


def test_cleanup_uses_session_retention_days_env_var_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_RETENTION_DAYS", "3")
    _env(monkeypatch, tmp_path)
    _seed_session(str(tmp_path / "viva.db"), "sess1", days_old=5)

    # No --older-than passed -- should fall back to SESSION_RETENTION_DAYS=3,
    # so a 5-day-old session is swept.
    result = runner.invoke(app, ["cleanup"])

    assert result.exit_code == 0
    assert "Removed 1 session(s)" in result.stdout


def test_cleanup_older_than_zero_exits_2(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["cleanup", "--older-than", "0"])

    assert result.exit_code == 2


def test_cleanup_missing_llm_model_exits_2(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "viva.db"))

    result = runner.invoke(app, ["cleanup"])

    assert result.exit_code == 2


def test_cleanup_all_removes_recent_sessions_too(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _seed_session(str(tmp_path / "viva.db"), "sess1")
    _seed_session(str(tmp_path / "viva.db"), "sess2")

    result = runner.invoke(app, ["cleanup", "--all"])

    assert result.exit_code == 0
    assert "Removed 2 session(s)" in result.stdout
    assert "0 session(s) retained." in result.stdout


def test_cleanup_shared_collection_survives_when_one_session_remains(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    db_path = str(tmp_path / "viva.db")
    collection = "o--r-abc123def456"
    vector_store = VectorStore(str(tmp_path / "chroma"))
    vector_store.upsert_chunks(collection, [_chunk("c1")], [[0.1, 0.2, 0.3]])
    _seed_session(db_path, "sess1", days_old=10, collection_name=collection)
    _seed_session(db_path, "sess2", collection_name=collection)

    result = runner.invoke(app, ["cleanup", "--older-than", "7"])

    assert result.exit_code == 0
    assert "Removed 1 session(s), 0 vector collection(s)" in result.stdout
    assert vector_store.collection_exists(collection) is True
