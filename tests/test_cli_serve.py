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

# typer.rich_utils forces colored/boxed Rich rendering for --help output
# whenever it detects GITHUB_ACTIONS, FORCE_COLOR, or PY_COLORS in the
# environment (assumed to be for nicer-looking CI logs) -- which is
# exactly the environment this test suite runs in on CI, and turns
# result.output into ANSI escape codes plus box-drawing characters
# instead of the plain text a naive substring check expects (reported
# from a real CI run: this test passed locally, where none of those
# env vars are set, then failed on GitHub Actions). Typer's own
# documented escape hatch, _TYPER_FORCE_DISABLE_TERMINAL, unconditionally
# overrides all three triggers back off -- passed as part of this
# invoke's env rather than as a suite-wide fixture, since this is the
# only test in the project that asserts on rendered --help content at
# all.
_PLAIN_TERMINAL_ENV = {"_TYPER_FORCE_DISABLE_TERMINAL": "1"}


def test_serve_help_lists_host_and_port_options():
    result = runner.invoke(app, ["serve", "--help"], env=_PLAIN_TERMINAL_ENV)

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
