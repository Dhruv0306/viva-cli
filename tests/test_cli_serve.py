"""Tests for `viva serve` (docs/plan.md Phase 10, CLI contract addition,
docs/system-design/15-phase-10-web-ui-design.md \u00a715.7).

Deliberately thin: `serve` hands off to `uvicorn.run()`, which blocks
until interrupted -- not something a unit test should actually invoke.
Covers only the CLI-argument-wiring/config-error-exit-2 surface that's
actually `cli.py`'s own job here; `create_app()`'s behavior is covered by
`test_web_app.py`.
"""
from __future__ import annotations

from typer.testing import CliRunner

from viva.cli import app

runner = CliRunner()


def test_serve_help_lists_host_and_port_options():
    result = runner.invoke(app, ["serve", "--help"])

    assert result.exit_code == 0
    assert "--host" in result.output
    assert "--port" in result.output


def test_serve_config_error_exits_2(monkeypatch, tmp_path):
    # No .env / LLM_MODEL in this env -- Config.load() should reject
    # before ever importing uvicorn/create_app.
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 2


def test_serve_calls_uvicorn_run_with_host_and_port(mocker, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "viva.db"))
    fake_app = object()
    mocker.patch("viva.web.app.create_app", return_value=fake_app)
    run_mock = mocker.patch("uvicorn.run")

    result = runner.invoke(app, ["serve", "--host", "0.0.0.0", "--port", "9001"])

    assert result.exit_code == 0
    run_mock.assert_called_once_with(fake_app, host="0.0.0.0", port=9001)
