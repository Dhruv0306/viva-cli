"""Tests for Phase 17's CLI logging hygiene (docs/plan.md Phase 17):
httpx/httpcore get redirected to a per-day file under logs/ instead of
the console, with a 3-day retention sweep at CLI startup. Extended to
also redirect viva.questiongen.retrieval's own per-question INFO line
(Phase 16 §19.6.2) once real usage confirmed it was just as disruptive
to the live question/answer UI as httpx's lines were.

Every test runs with monkeypatch.chdir(tmp_path) first, since
_configure_logging() creates ./logs relative to the current working
directory (the same relative-path convention SESSION_DB_PATH's default
already uses) -- without isolating cwd, running this file would create
a real logs/ directory in whatever directory pytest happens to run
from.

Isolation for the loggers _configure_logging() mutates
(httpx/httpcore/viva.questiongen.retrieval) is handled by a suite-wide
autouse fixture in tests/conftest.py, not anything file-local here.
Two real bugs shipped before that fixture existed, in order: first, a
helper called manually at the start/end of each test body, where
anything raising before the trailing call skipped cleanup; then a
fixture scoped to just this file, where any of the other 10 test files
that also call runner.invoke(app, ...) -- none of which know this
global state exists -- could leave propagate=False behind for whichever
test ran next in the same process, wherever in the suite that landed
(observed live, twice: test_questiongen_retrieval.py's caplog-based
test coming back empty each time). Only a suite-wide fixture actually
closes that gap.
"""
from __future__ import annotations

import datetime as dt
import logging
import os

from typer.testing import CliRunner

import viva.cli
from viva.cli import app

runner = CliRunner()


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "viva.db"))
    monkeypatch.setenv("VECTOR_DB_PATH", str(tmp_path / "chroma"))


def test_conftest_noisy_logger_list_matches_cli_module():
    # conftest.py's suite-wide reset fixture duplicates this list rather
    # than importing viva.cli at collection time for every test file in
    # the suite (most of which have nothing to do with the CLI) -- this
    # guards against the two silently drifting apart, which would bring
    # back exactly the leak this file's own history is about, just for
    # a logger name added to one list and not the other.
    import conftest

    assert set(conftest._NOISY_LOGGER_NAMES) == set(viva.cli._NOISY_LOGGER_NAMES)


def test_httpx_logger_stops_propagating_to_the_console(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["cleanup"])

    assert result.exit_code == 0
    assert logging.getLogger("httpx").propagate is False
    assert logging.getLogger("httpcore").propagate is False


def test_questiongen_retrieval_logger_stops_propagating_to_the_console(monkeypatch, tmp_path):
    # Joined httpx/httpcore on this list once real usage confirmed it
    # was just as disruptive: one line per question generated,
    # interleaved with the live question/answer UI.
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["cleanup"])

    assert result.exit_code == 0
    assert logging.getLogger("viva.questiongen.retrieval").propagate is False


def test_questiongen_retrieval_logger_writes_to_a_dated_log_file(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["cleanup"])
    logging.getLogger("viva.questiongen.retrieval").info(
        "Retrieval for category=architecture architecture_topic=overview "
        "target_module=None target_file=None: 15 candidate(s) fetched, "
        "15 after test-path filter, 5 after relevance filter "
        "(distances: min=0.583 max=0.626)"
    )

    expected_path = tmp_path / "logs" / f"log_{dt.date.today():%Y_%m_%d}.log"
    assert expected_path.exists()
    assert "Retrieval for category=architecture" in expected_path.read_text()


def test_questiongen_retrieval_lines_do_not_reach_stdout(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["cleanup"])
    logging.getLogger("viva.questiongen.retrieval").info(
        "Retrieval for category=architecture architecture_topic=overview "
        "target_module=None target_file=None: 15 candidate(s) fetched, "
        "15 after test-path filter, 5 after relevance filter "
        "(distances: min=0.583 max=0.626)"
    )
    result = runner.invoke(app, ["cleanup"])

    assert "Retrieval for category=" not in result.stdout


def test_httpx_logger_writes_to_a_dated_log_file(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["cleanup"])
    logging.getLogger("httpx").info("HTTP Request: POST http://localhost:11434/api/chat")

    expected_path = tmp_path / "logs" / f"log_{dt.date.today():%Y_%m_%d}.log"
    assert expected_path.exists()
    assert "HTTP Request: POST http://localhost:11434/api/chat" in expected_path.read_text()


def test_httpx_lines_do_not_reach_stdout(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["cleanup"])
    logging.getLogger("httpx").info("HTTP Request: POST http://localhost:11434/api/chat")
    result = runner.invoke(app, ["cleanup"])

    assert "HTTP Request" not in result.stdout


def test_orchestrator_logger_still_reaches_the_console(monkeypatch, tmp_path):
    # Phase 17's own exit criteria (docs/plan.md): this phase must not
    # regress Phase 13's planning-decision log line the same way it
    # silences httpx/httpcore/viva.questiongen.retrieval.
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    assert logging.getLogger("viva.orchestrator").propagate is True


def test_stale_log_files_are_removed_at_startup(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    stale = logs_dir / "log_2020_01_01.log"
    stale.write_text("old log content")
    old_time = dt.datetime.now().timestamp() - (10 * 24 * 60 * 60)
    os.utime(stale, (old_time, old_time))

    runner.invoke(app, ["cleanup"])

    assert not stale.exists()


def test_recent_log_files_are_kept(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    recent = logs_dir / "log_2020_01_01.log"
    recent.write_text("recent log content")
    recent_time = dt.datetime.now().timestamp() - (1 * 24 * 60 * 60)
    os.utime(recent, (recent_time, recent_time))

    runner.invoke(app, ["cleanup"])

    assert recent.exists()
