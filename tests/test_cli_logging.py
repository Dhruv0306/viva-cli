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
"""
from __future__ import annotations

import datetime as dt
import logging

from typer.testing import CliRunner

from viva.cli import app

runner = CliRunner()

_REDIRECTED_LOGGER_NAMES = ("httpx", "httpcore", "viva.questiongen.retrieval")


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "viva.db"))
    monkeypatch.setenv("VECTOR_DB_PATH", str(tmp_path / "chroma"))


def _reset_noisy_loggers():
    # _configure_logging() runs on every CLI invocation within a single
    # test process (unlike a real `viva` process, which only runs it
    # once) -- without resetting handlers between tests, FileHandlers
    # from earlier tests accumulate on the same logger objects (they're
    # module-level singletons via logging.getLogger()) and every
    # assertion below would see stale state from a previous test's
    # tmp_path.
    for name in _REDIRECTED_LOGGER_NAMES:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        logger.propagate = True
        logger.setLevel(logging.NOTSET)


def test_httpx_logger_stops_propagating_to_the_console(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _reset_noisy_loggers()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["cleanup"])

    assert result.exit_code == 0
    assert logging.getLogger("httpx").propagate is False
    assert logging.getLogger("httpcore").propagate is False
    _reset_noisy_loggers()


def test_questiongen_retrieval_logger_stops_propagating_to_the_console(monkeypatch, tmp_path):
    # Joined httpx/httpcore on this list once real usage confirmed it
    # was just as disruptive: one line per question generated,
    # interleaved with the live question/answer UI.
    _env(monkeypatch, tmp_path)
    _reset_noisy_loggers()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["cleanup"])

    assert result.exit_code == 0
    assert logging.getLogger("viva.questiongen.retrieval").propagate is False
    _reset_noisy_loggers()


def test_questiongen_retrieval_logger_writes_to_a_dated_log_file(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _reset_noisy_loggers()
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
    _reset_noisy_loggers()


def test_questiongen_retrieval_lines_do_not_reach_stdout(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _reset_noisy_loggers()
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
    _reset_noisy_loggers()


def test_httpx_logger_writes_to_a_dated_log_file(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _reset_noisy_loggers()
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["cleanup"])
    logging.getLogger("httpx").info("HTTP Request: POST http://localhost:11434/api/chat")

    expected_path = tmp_path / "logs" / f"log_{dt.date.today():%Y_%m_%d}.log"
    assert expected_path.exists()
    assert "HTTP Request: POST http://localhost:11434/api/chat" in expected_path.read_text()
    _reset_noisy_loggers()


def test_httpx_lines_do_not_reach_stdout(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _reset_noisy_loggers()
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["cleanup"])
    logging.getLogger("httpx").info("HTTP Request: POST http://localhost:11434/api/chat")
    result = runner.invoke(app, ["cleanup"])

    assert "HTTP Request" not in result.stdout
    _reset_noisy_loggers()


def test_orchestrator_logger_still_reaches_the_console(monkeypatch, tmp_path):
    # Phase 17's own exit criteria (docs/plan.md): this phase must not
    # regress Phase 13's planning-decision log line the same way it
    # silences httpx -- only httpx/httpcore are redirected.
    _env(monkeypatch, tmp_path)
    _reset_noisy_loggers()
    monkeypatch.chdir(tmp_path)

    assert logging.getLogger("viva.orchestrator").propagate is True
    _reset_noisy_loggers()


def test_stale_log_files_are_removed_at_startup(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _reset_noisy_loggers()
    monkeypatch.chdir(tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    stale = logs_dir / "log_2020_01_01.log"
    stale.write_text("old log content")
    old_time = dt.datetime.now().timestamp() - (10 * 24 * 60 * 60)
    import os
    os.utime(stale, (old_time, old_time))

    runner.invoke(app, ["cleanup"])

    assert not stale.exists()
    _reset_noisy_loggers()


def test_recent_log_files_are_kept(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _reset_noisy_loggers()
    monkeypatch.chdir(tmp_path)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    recent = logs_dir / "log_2020_01_01.log"
    recent.write_text("recent log content")
    recent_time = dt.datetime.now().timestamp() - (1 * 24 * 60 * 60)
    import os
    os.utime(recent, (recent_time, recent_time))

    runner.invoke(app, ["cleanup"])

    assert recent.exists()
    _reset_noisy_loggers()
