"""Tests for the `viva analyze` CLI command.

`ingest_repo`/`analyze_repo` are mocked at the point cli.py imports
them -- this exercises argument wiring, output formatting, and the
profile-JSON write, not the pipelines themselves (covered by
tests/test_ingest_integration.py and tests/test_analyzer_integration.py).
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from viva.analyzer.models import AnalysisResult, AnalysisStats, ModuleSummary
from viva.cli import app
from viva.ingest.clone import CloneError
from viva.ingest.models import IngestResult

runner = CliRunner()


def _fake_ingest_result() -> IngestResult:
    return IngestResult(
        repo_url="https://github.com/owner/repo", repo_slug="owner/repo", commit_sha="abc123def456",
        branch="main", local_path=Path("/tmp/fake-clone"), files_total=12, files_analyzed=12,
        sampled_files=[], excluded_notable=[], sampling_note="analyzed 12/12 files, no sampling needed",
        detected_stack=["python"],
    )


def _fake_analysis_result() -> AnalysisResult:
    return AnalysisResult(
        architecture_summary="A small python CLI tool.",
        modules=[ModuleSummary(module="src", summary="core logic", file_count=4)],
        entry_points=["src/main.py"],
        test_coverage_present=True,
        analysis_stats=AnalysisStats(files_analyzed=12, ast_parsed=8, line_window_fallback=4),
    )


def test_analyze_command_reports_success_and_writes_profile(mocker, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    mocker.patch("viva.cli.ingest_repo", return_value=_fake_ingest_result())
    mocker.patch("viva.cli.analyze_repo", return_value=_fake_analysis_result())
    output_path = tmp_path / "profile.json"

    result = runner.invoke(app, ["analyze", "https://github.com/owner/repo", "--output", str(output_path)])

    assert result.exit_code == 0
    assert "A small python CLI tool." in result.stdout
    assert "src" in result.stdout
    assert "src/main.py" in result.stdout
    assert output_path.exists()

    written = json.loads(output_path.read_text())
    assert written["repo_slug"] == "owner/repo"
    assert written["architecture_summary"] == "A small python CLI tool."
    assert written["modules"][0]["module"] == "src"


def test_analyze_command_reports_clone_failure(mocker, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    mocker.patch("viva.cli.ingest_repo", side_effect=CloneError("Authentication failed"))

    result = runner.invoke(app, ["analyze", "https://github.com/owner/private-repo"])

    assert result.exit_code == 1
    assert "Clone failed" in result.stdout


def test_analyze_command_reports_config_error(mocker, monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    mocker.patch("viva.config.load_dotenv")

    result = runner.invoke(app, ["analyze", "https://github.com/owner/repo"])

    assert result.exit_code == 2
    assert "Configuration error" in result.stdout


def test_analyze_command_reports_analysis_failure(mocker, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    mocker.patch("viva.cli.ingest_repo", return_value=_fake_ingest_result())
    mocker.patch("viva.cli.analyze_repo", side_effect=RuntimeError("ollama connection refused"))

    result = runner.invoke(app, ["analyze", "https://github.com/owner/repo"])

    assert result.exit_code == 1
    assert "Analysis failed" in result.stdout
