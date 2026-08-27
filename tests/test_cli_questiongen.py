"""Tests for the `viva questiongen` CLI command.

`ingest_repo`/`analyze_repo`/`index_repo`/`generate_all` are mocked at
the point cli.py imports them -- exercises argument wiring and output
formatting, not the pipelines themselves (covered by
test_questiongen_integration.py), following the same pattern as
test_cli_index.py.
"""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from viva.analyzer.models import AnalysisResult, AnalysisStats, ModuleSummary
from viva.cli import app
from viva.indexer.models import IndexResult, IndexStats
from viva.ingest.models import IngestResult
from viva.questiongen.models import GeneratedQuestion, QuestionGenStats, QuestionPlanItem

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


def _fake_index_result() -> IndexResult:
    return IndexResult(
        collection_name="owner--repo-abc123def456",
        stats=IndexStats(files_processed=12, chunks_built=37, reused_existing_collection=False),
    )


def _fake_questions() -> tuple[list[GeneratedQuestion], QuestionGenStats]:
    questions = [
        GeneratedQuestion(
            plan_item=QuestionPlanItem(id="q_01", category="implementation_detail", target_module="src"),
            question_text="How does the retry logic in this module handle a timeout?",
            grounding_chunk_ids=["c1", "c2"],
        ),
        GeneratedQuestion(
            plan_item=QuestionPlanItem(id="q_02", category="architecture", target_module=None),
            question_text="Why is the CLI split into these components?",
            grounding_chunk_ids=["c3"],
        ),
    ]
    stats = QuestionGenStats(plan_items_built=3, questions_generated=2, plan_items_skipped_no_grounding=1)
    return questions, stats


def test_questiongen_command_reports_success(mocker, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    mocker.patch("viva.cli.ingest_repo", return_value=_fake_ingest_result())
    mocker.patch("viva.cli.analyze_repo", return_value=_fake_analysis_result())
    mocker.patch("viva.cli.index_repo", return_value=_fake_index_result())
    mocker.patch("viva.cli.generate_all", return_value=_fake_questions())

    result = runner.invoke(app, ["questiongen", "https://github.com/owner/repo"])

    assert result.exit_code == 0
    assert "3 planned" in result.stdout
    assert "2 generated" in result.stdout
    assert "1 skipped" in result.stdout


def test_questiongen_command_prints_category_and_module_label(mocker, monkeypatch):
    # Regression test: the category/module label was originally built as
    # a bare f"[{category} / {module}]" string passed straight to
    # console.print(), which Rich silently swallows as unrecognized
    # markup instead of printing literally -- confirmed against a real
    # `viva questiongen` run against pallets/click, where every
    # question's label was missing from the output entirely.
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    mocker.patch("viva.cli.ingest_repo", return_value=_fake_ingest_result())
    mocker.patch("viva.cli.analyze_repo", return_value=_fake_analysis_result())
    mocker.patch("viva.cli.index_repo", return_value=_fake_index_result())
    mocker.patch("viva.cli.generate_all", return_value=_fake_questions())

    result = runner.invoke(app, ["questiongen", "https://github.com/owner/repo"])

    assert result.exit_code == 0
    assert "implementation_detail / src" in result.stdout
    assert "architecture / (project-level)" in result.stdout


def test_questiongen_command_prints_question_text_and_grounding(mocker, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    mocker.patch("viva.cli.ingest_repo", return_value=_fake_ingest_result())
    mocker.patch("viva.cli.analyze_repo", return_value=_fake_analysis_result())
    mocker.patch("viva.cli.index_repo", return_value=_fake_index_result())
    mocker.patch("viva.cli.generate_all", return_value=_fake_questions())

    result = runner.invoke(app, ["questiongen", "https://github.com/owner/repo"])

    assert "How does the retry logic in this module handle a timeout?" in result.stdout
    assert "c1" in result.stdout and "c2" in result.stdout
