"""Tests for the `viva index` CLI command.

`ingest_repo`/`analyze_repo`/`index_repo` are mocked at the point cli.py
imports them -- this exercises argument wiring and output formatting,
not the pipelines themselves (covered by test_ingest_integration.py,
test_analyzer_integration.py, test_indexer_integration.py), following
the same pattern as test_cli_analyze.py.
"""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from viva.analyzer.models import AnalysisResult, AnalysisStats, ModuleSummary
from viva.cli import app
from viva.indexer.models import IndexResult, IndexStats
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


def _fake_index_result(reused: bool = False) -> IndexResult:
    return IndexResult(
        collection_name="owner--repo-abc123def456",
        stats=IndexStats(files_processed=0 if reused else 12, chunks_built=0 if reused else 37, reused_existing_collection=reused),
    )


def test_index_command_reports_success(mocker, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    mocker.patch("viva.cli.ingest_repo", return_value=_fake_ingest_result())
    mocker.patch("viva.cli.analyze_repo", return_value=_fake_analysis_result())
    mocker.patch("viva.cli.index_repo", return_value=_fake_index_result())

    result = runner.invoke(app, ["index", "https://github.com/owner/repo"])

    assert result.exit_code == 0
    assert "owner--repo-abc123def456" in result.stdout
    assert "Chunks indexed" in result.stdout
    assert "37" in result.stdout


def test_index_command_reports_reuse(mocker, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    mocker.patch("viva.cli.ingest_repo", return_value=_fake_ingest_result())
    mocker.patch("viva.cli.analyze_repo", return_value=_fake_analysis_result())
    mocker.patch("viva.cli.index_repo", return_value=_fake_index_result(reused=True))

    result = runner.invoke(app, ["index", "https://github.com/owner/repo"])

    assert result.exit_code == 0
    assert "Reused existing collection" in result.stdout


def test_index_command_runs_sample_query_when_requested(mocker, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    mocker.patch("viva.cli.ingest_repo", return_value=_fake_ingest_result())
    mocker.patch("viva.cli.analyze_repo", return_value=_fake_analysis_result())
    mocker.patch("viva.cli.index_repo", return_value=_fake_index_result())
    mocker.patch("viva.cli.OllamaEmbeddingClient.embed", return_value=[[0.1, 0.2]])
    mocker.patch(
        "viva.cli.VectorStore.query",
        return_value=[
            {
                "id": "c1",
                "text": "def login(conn, user):\n    ...",
                "metadata": {
                    "filepath": "src/app/auth/handlers.py", "start_line": 1, "end_line": 3,
                    "symbol_name": "login", "kind": "function", "parse_method": "ast",
                },
                "distance": 0.05,
            }
        ],
    )

    result = runner.invoke(app, ["index", "https://github.com/owner/repo", "--query", "how is login handled"])

    assert result.exit_code == 0
    assert "Retrieval query" in result.stdout
    assert "src/app/auth/handlers.py:1-3" in result.stdout
    assert "login" in result.stdout


def test_index_command_no_query_skips_retrieval_section(mocker, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    mocker.patch("viva.cli.ingest_repo", return_value=_fake_ingest_result())
    mocker.patch("viva.cli.analyze_repo", return_value=_fake_analysis_result())
    mocker.patch("viva.cli.index_repo", return_value=_fake_index_result())
    query_mock = mocker.patch("viva.cli.VectorStore.query")

    result = runner.invoke(app, ["index", "https://github.com/owner/repo"])

    assert result.exit_code == 0
    assert "Retrieval query" not in result.stdout
    query_mock.assert_not_called()


def test_index_command_reports_clone_failure(mocker, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    mocker.patch("viva.cli.ingest_repo", side_effect=CloneError("Authentication failed"))

    result = runner.invoke(app, ["index", "https://github.com/owner/private-repo"])

    assert result.exit_code == 1
    assert "Clone failed" in result.stdout


def test_index_command_reports_config_error(mocker, monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    mocker.patch("viva.config.load_dotenv")

    result = runner.invoke(app, ["index", "https://github.com/owner/repo"])

    assert result.exit_code == 2
    assert "Configuration error" in result.stdout


def test_index_command_reports_indexing_failure(mocker, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    mocker.patch("viva.cli.ingest_repo", return_value=_fake_ingest_result())
    mocker.patch("viva.cli.analyze_repo", return_value=_fake_analysis_result())
    mocker.patch("viva.cli.index_repo", side_effect=RuntimeError("ollama connection refused"))

    result = runner.invoke(app, ["index", "https://github.com/owner/repo"])

    assert result.exit_code == 1
    assert "Indexing failed" in result.stdout
