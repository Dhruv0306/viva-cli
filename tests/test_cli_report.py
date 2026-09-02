"""Tests for `viva report` (docs/plan.md Phase 8, CLI contract §6.1,
docs/system-design/13-phase-8-report-design.md §13.7).

Exercises a real `SessionStore` against a tmp DB, following
`test_cli_session.py`'s `list` pattern -- this command's whole job is
reading and formatting what's actually persisted, so there's nothing
worth mocking.
"""
from __future__ import annotations

import json

from typer.testing import CliRunner

from viva.cli import app
from viva.schemas import EvaluationRecord
from viva.storage import SessionStore

runner = CliRunner()


def _seed_completed_session(db_path: str) -> None:
    store = SessionStore(db_path)
    store.create_session("sess1", "https://github.com/o/r", "main", "demo", 1800)
    store.set_pipeline_artifacts(
        "sess1", repo_slug="o/r", commit_sha="abc123def456",
        collection_name="o--r-abc123def456", profile_path="/tmp/p.json",
    )
    from viva.questiongen.models import QuestionPlanItem

    store.save_plan("sess1", [QuestionPlanItem(id="q1", category="architecture", target_module=None)])
    store.record_question_asked("sess1", "q1", "How does the Orchestrator work?", [])
    store.record_answer("sess1", "q1", "It mediates every component.")
    record = EvaluationRecord(
        classification="correct",
        summary="Correctly described the mediator role.",
        cited_file="src/viva/orchestrator.py",
        did_well=["Named the Orchestrator's mediator role correctly."],
        missed=[],
        did_wrong=[],
        improvement="None needed.",
        needs_review=False,
    )
    store.set_eval_complete("sess1", "q1", record.model_dump_json(), needs_review=False)
    store.update_status("sess1", "COMPLETE")
    store.close()


def test_report_not_found_exits_3(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "viva.db"))

    result = runner.invoke(app, ["report", "does-not-exist"])

    assert result.exit_code == 3


def test_report_on_incomplete_session_exits_3_without_allow_partial(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    db_path = str(tmp_path / "viva.db")
    monkeypatch.setenv("SESSION_DB_PATH", db_path)
    store = SessionStore(db_path)
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.update_status("sess1", "IN_PROGRESS")
    store.close()

    result = runner.invoke(app, ["report", "sess1"])

    assert result.exit_code == 3
    assert "not COMPLETE" in result.stdout


def test_report_on_incomplete_session_with_allow_partial_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    db_path = str(tmp_path / "viva.db")
    monkeypatch.setenv("SESSION_DB_PATH", db_path)
    store = SessionStore(db_path)
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.update_status("sess1", "IN_PROGRESS")
    store.close()

    result = runner.invoke(app, ["report", "sess1", "--allow-partial"])

    assert result.exit_code == 0
    assert "# Viva Report" in result.stdout


def test_report_markdown_is_default(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    db_path = str(tmp_path / "viva.db")
    monkeypatch.setenv("SESSION_DB_PATH", db_path)
    _seed_completed_session(db_path)

    result = runner.invoke(app, ["report", "sess1"])

    assert result.exit_code == 0
    assert "# Viva Report" in result.stdout
    assert "Named the Orchestrator's mediator role correctly." in result.stdout


def test_report_json_format(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    db_path = str(tmp_path / "viva.db")
    monkeypatch.setenv("SESSION_DB_PATH", db_path)
    _seed_completed_session(db_path)

    result = runner.invoke(app, ["report", "sess1", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["session_id"] == "sess1"
    assert payload["answered_count"] == 1
    assert payload["questions"][0]["classification"] == "correct"


def test_report_invalid_format_exits_2(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    db_path = str(tmp_path / "viva.db")
    monkeypatch.setenv("SESSION_DB_PATH", db_path)
    _seed_completed_session(db_path)

    result = runner.invoke(app, ["report", "sess1", "--format", "yaml"])

    assert result.exit_code == 2


def test_report_output_writes_to_file(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    db_path = str(tmp_path / "viva.db")
    monkeypatch.setenv("SESSION_DB_PATH", db_path)
    _seed_completed_session(db_path)
    out_path = tmp_path / "report.md"

    result = runner.invoke(app, ["report", "sess1", "--output", str(out_path)])

    assert result.exit_code == 0
    assert out_path.exists()
    assert "# Viva Report" in out_path.read_text(encoding="utf-8")
