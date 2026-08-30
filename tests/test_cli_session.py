"""Tests for `viva start` / `viva resume` / `viva list` (docs/plan.md
Phase 6, CLI contract §6.1).

`start`/`resume` mock `viva.cli.Orchestrator` itself -- the pipeline it
drives is covered by `test_orchestrator.py`; these tests exercise CLI
argument wiring, exit codes, and error-message routing only, following
the same pattern as `test_cli_questiongen.py`. `list` exercises a real
`SessionStore` against a tmp DB, since its whole job is formatting what's
actually persisted.
"""
from __future__ import annotations

from typer.testing import CliRunner

from viva.cli import app
from viva.ingest.clone import CloneError
from viva.orchestrator import (
    OrchestratorError,
    SessionAlreadyCompleteError,
    SessionNotFoundError,
    SessionNotResumableError,
)
from viva.storage import SessionStore

runner = CliRunner()


class _FakeOrchestrator:
    """Stand-in for `Orchestrator` swapped in via `mocker.patch`."""

    def __init__(self, start_result=None, start_exc=None, resume_exc=None, **_kw):
        self._start_result = start_result
        self._start_exc = start_exc
        self._resume_exc = resume_exc
        self.start_calls = []
        self.resume_calls = []

    def start(self, repo_url, branch=None, duration_minutes=None, session_name=None):
        self.start_calls.append((repo_url, branch, duration_minutes, session_name))
        if self._start_exc:
            raise self._start_exc
        return self._start_result or "sess123"

    def resume(self, session_id):
        self.resume_calls.append(session_id)
        if self._resume_exc:
            raise self._resume_exc


def _patch_env_and_store(monkeypatch, mocker, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "viva.db"))
    # SessionStore is real (cheap, local sqlite) so `store.close()` in
    # cli.py's `finally` block has something real to call.
    return mocker.patch("viva.cli.SessionStore", wraps=SessionStore)


def test_start_success_exits_zero(mocker, monkeypatch, tmp_path):
    _patch_env_and_store(monkeypatch, mocker, tmp_path)
    fake = _FakeOrchestrator(start_result="sess123")
    mocker.patch("viva.cli.Orchestrator", return_value=fake)

    result = runner.invoke(app, ["start", "https://github.com/owner/repo", "--branch", "main"])

    assert result.exit_code == 0
    assert fake.start_calls == [("https://github.com/owner/repo", "main", None, None)]


def test_start_passes_duration_and_session_name(mocker, monkeypatch, tmp_path):
    _patch_env_and_store(monkeypatch, mocker, tmp_path)
    fake = _FakeOrchestrator()
    mocker.patch("viva.cli.Orchestrator", return_value=fake)

    result = runner.invoke(
        app,
        ["start", "https://github.com/owner/repo", "--duration", "45", "--session-name", "demo"],
    )

    assert result.exit_code == 0
    assert fake.start_calls == [("https://github.com/owner/repo", None, 45, "demo")]


def test_start_clone_error_exits_2(mocker, monkeypatch, tmp_path):
    _patch_env_and_store(monkeypatch, mocker, tmp_path)
    fake = _FakeOrchestrator(start_exc=CloneError("bad url"))
    mocker.patch("viva.cli.Orchestrator", return_value=fake)

    result = runner.invoke(app, ["start", "not-a-real-url"])

    assert result.exit_code == 2


def test_start_orchestrator_error_exits_1(mocker, monkeypatch, tmp_path):
    _patch_env_and_store(monkeypatch, mocker, tmp_path)
    fake = _FakeOrchestrator(start_exc=OrchestratorError("setup failed"))
    mocker.patch("viva.cli.Orchestrator", return_value=fake)

    result = runner.invoke(app, ["start", "https://github.com/owner/repo"])

    assert result.exit_code == 1


def test_start_missing_config_exits_2(mocker, monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    mocker.patch("viva.config.load_dotenv")  # don't let a real .env override the missing var

    result = runner.invoke(app, ["start", "https://github.com/owner/repo"])

    assert result.exit_code == 2


def test_resume_success_exits_zero(mocker, monkeypatch, tmp_path):
    _patch_env_and_store(monkeypatch, mocker, tmp_path)
    fake = _FakeOrchestrator()
    mocker.patch("viva.cli.Orchestrator", return_value=fake)

    result = runner.invoke(app, ["resume", "sess123"])

    assert result.exit_code == 0
    assert fake.resume_calls == ["sess123"]


def test_resume_not_found_exits_3(mocker, monkeypatch, tmp_path):
    _patch_env_and_store(monkeypatch, mocker, tmp_path)
    fake = _FakeOrchestrator(resume_exc=SessionNotFoundError("no such session"))
    mocker.patch("viva.cli.Orchestrator", return_value=fake)

    result = runner.invoke(app, ["resume", "nope"])

    assert result.exit_code == 3


def test_resume_already_complete_exits_3(mocker, monkeypatch, tmp_path):
    _patch_env_and_store(monkeypatch, mocker, tmp_path)
    fake = _FakeOrchestrator(resume_exc=SessionAlreadyCompleteError("already done"))
    mocker.patch("viva.cli.Orchestrator", return_value=fake)

    result = runner.invoke(app, ["resume", "sess123"])

    assert result.exit_code == 3
    assert "already done" in result.stdout


def test_resume_not_resumable_exits_3(mocker, monkeypatch, tmp_path):
    _patch_env_and_store(monkeypatch, mocker, tmp_path)
    fake = _FakeOrchestrator(resume_exc=SessionNotResumableError("crashed early"))
    mocker.patch("viva.cli.Orchestrator", return_value=fake)

    result = runner.invoke(app, ["resume", "sess123"])

    assert result.exit_code == 3


def test_list_empty_shows_message(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "viva.db"))

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "No sessions found" in result.stdout


def test_list_shows_persisted_sessions(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    db_path = str(tmp_path / "viva.db")
    monkeypatch.setenv("SESSION_DB_PATH", db_path)

    store = SessionStore(db_path)
    store.create_session("sess1", "https://github.com/o/r", "main", "demo", 1800)
    store.set_pipeline_artifacts("sess1", repo_slug="o/r", commit_sha="abc123def456",
                                  collection_name="o--r-abc123def456", profile_path="/tmp/p.json")
    store.update_status("sess1", "IN_PROGRESS")
    store.close()

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "sess1" in result.stdout
    assert "o/r" in result.stdout
    assert "IN_PROGRESS" in result.stdout


def test_list_filters_by_status(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    db_path = str(tmp_path / "viva.db")
    monkeypatch.setenv("SESSION_DB_PATH", db_path)

    store = SessionStore(db_path)
    store.create_session("sess1", "https://github.com/o/r1", None, None, 1800)
    store.create_session("sess2", "https://github.com/o/r2", None, None, 1800)
    store.update_status("sess2", "COMPLETE")
    store.close()

    result = runner.invoke(app, ["list", "--status", "COMPLETE"])

    assert result.exit_code == 0
    assert "sess2" in result.stdout
    assert "sess1" not in result.stdout
