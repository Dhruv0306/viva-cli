"""Tests for the `viva ingest` CLI command.

`ingest_repo` is mocked at the point cli.py imports it -- this test
exercises argument wiring and output formatting only, not the ingestion
pipeline itself (covered by tests/test_ingest_integration.py).
"""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from viva.cli import app
from viva.ingest.clone import CloneError
from viva.ingest.models import IngestResult

runner = CliRunner()


def _fake_result() -> IngestResult:
    return IngestResult(
        repo_url="https://github.com/owner/repo",
        repo_slug="owner/repo",
        commit_sha="abc123def456",
        branch="main",
        local_path=Path("/tmp/fake-clone"),
        files_total=12,
        files_analyzed=12,
        sampled_files=[],
        excluded_notable=[],
        sampling_note="analyzed 12/12 files, no sampling needed",
        detected_stack=["python"],
    )


def test_ingest_command_reports_success(mocker, monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    mocker.patch("viva.cli.ingest_repo", return_value=_fake_result())

    result = runner.invoke(app, ["ingest", "https://github.com/owner/repo"])

    assert result.exit_code == 0
    assert "owner/repo" in result.stdout
    assert "python" in result.stdout
    assert "12/12" in result.stdout


def test_ingest_command_reports_clone_failure(mocker, monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    mocker.patch("viva.cli.ingest_repo", side_effect=CloneError("Authentication failed"))

    result = runner.invoke(app, ["ingest", "https://github.com/owner/private-repo"])

    assert result.exit_code == 1
    assert "Clone failed" in result.stdout


def test_ingest_command_reports_config_error(mocker, monkeypatch) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)
    mocker.patch("viva.config.load_dotenv")  # don't let a real .env override the missing var

    result = runner.invoke(app, ["ingest", "https://github.com/owner/repo"])

    assert result.exit_code == 2
    assert "Configuration error" in result.stdout
