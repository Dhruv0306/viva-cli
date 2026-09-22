"""Tests for `viva doctor` (docs/plan.md Phase 20, docs/system-design/
25-phase-20-serve-hardening-onboarding-design.md §25.3).

`ollama.Client` is mocked at the point `cli.doctor()` constructs it
(`viva.cli.ollama.Client`) -- there's no `OllamaClient` wrapper instance
to reach into here (unlike test_llm_client.py's `c._client = MagicMock()`
pattern), since `doctor()` builds a raw `ollama.Client` inline for a
single reachability probe, deliberately not the LLM-call abstraction.
"""
from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from viva.cli import app

runner = CliRunner()


def _env(monkeypatch, llm_model="gemma4:e4b", embedding_model="nomic-embed-text"):
    monkeypatch.setenv("LLM_MODEL", llm_model)
    monkeypatch.setenv("EMBEDDING_MODEL", embedding_model)


def _fake_list_response(*model_tags: str) -> SimpleNamespace:
    return SimpleNamespace(models=[SimpleNamespace(model=tag) for tag in model_tags])


def test_doctor_reports_config_error_and_exits_2(mocker, monkeypatch):
    # LLM_MODEL deliberately not set -- mirrors every other command's
    # existing ConfigError handling (cleanup, start, ...), not a new
    # message of doctor's own.
    monkeypatch.delenv("LLM_MODEL", raising=False)
    # A real .env file on the machine running this test (e.g. a dev
    # checkout with .env.example copied to .env, per the README) would
    # otherwise refill LLM_MODEL right back in -- load_dotenv() doesn't
    # override an explicitly-set env var, but monkeypatch.delenv leaves
    # it *unset*, which is exactly what load_dotenv fills in from disk.
    # Caught by a real run on real dev hardware, not by this sandbox
    # (no .env file here to expose it) -- same fix
    # test_cli_cleanup.py::test_cleanup_missing_llm_model_exits_2 and
    # test_cli_session.py::test_start_missing_config_exits_2 already
    # apply for this exact reason.
    mocker.patch("viva.config.load_dotenv")

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 2
    assert "Configuration error" in result.stdout


def test_doctor_all_checks_pass_exits_0(monkeypatch, mocker):
    _env(monkeypatch)
    fake_client = mocker.MagicMock()
    fake_client.list.return_value = _fake_list_response("gemma4:e4b", "nomic-embed-text")
    mocker.patch("viva.cli.ollama.Client", return_value=fake_client)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "Configuration loaded" in result.stdout
    assert "Ollama reachable" in result.stdout
    assert "LLM_MODEL 'gemma4:e4b' is pulled" in result.stdout
    assert "EMBEDDING_MODEL 'nomic-embed-text' is pulled" in result.stdout


def test_doctor_missing_model_exits_1(monkeypatch, mocker):
    _env(monkeypatch)
    fake_client = mocker.MagicMock()
    # LLM_MODEL pulled, EMBEDDING_MODEL isn't.
    fake_client.list.return_value = _fake_list_response("gemma4:e4b")
    mocker.patch("viva.cli.ollama.Client", return_value=fake_client)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "LLM_MODEL 'gemma4:e4b' is pulled" in result.stdout
    assert "EMBEDDING_MODEL 'nomic-embed-text' is not pulled" in result.stdout
    assert "ollama pull nomic-embed-text" in result.stdout


def test_doctor_connection_refused_exits_1(monkeypatch, mocker):
    # "Ollama not running at all" -- the common case. ollama's own code
    # wraps this as a plain builtin ConnectionError with a friendly
    # message, confirmed directly (design doc §25.3), not assumed.
    _env(monkeypatch)
    mocker.patch(
        "viva.cli.ollama.Client",
        side_effect=ConnectionError(
            "Failed to connect to Ollama. Please check that Ollama is "
            "downloaded, running and accessible."
        ),
    )

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "Ollama not reachable" in result.stdout
    assert "could not check" in result.stdout


def test_doctor_connection_timeout_exits_1(monkeypatch, mocker):
    # The specific gap design doc §25.3 exists to close: a genuine
    # network timeout raises a *different*, unwrapped exception type
    # than a refused connection (confirmed directly against a real
    # client: httpx.ConnectTimeout, not ConnectionError). A test that
    # only covers test_doctor_connection_refused_exits_1 above would
    # miss this entirely -- doctor() must catch bare Exception around
    # the reachability call, not just ConnectionError, or this case
    # produces an unhandled traceback instead of a clean report.
    _env(monkeypatch)

    class _TimeoutShapedError(Exception):
        """Stands in for httpx.ConnectTimeout without importing httpx
        here -- what matters for this test is that it's an Exception
        subclass that ISN'T ConnectionError, exercising the bare-except
        path rather than the narrower one."""

    mocker.patch(
        "viva.cli.ollama.Client", side_effect=_TimeoutShapedError("timed out")
    )

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "Ollama not reachable" in result.stdout
