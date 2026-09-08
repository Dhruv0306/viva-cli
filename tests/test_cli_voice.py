"""Tests for `viva voice setup` (docs/plan.md Phase 11, CLI contract
addition, docs/system-design/16-phase-11-voice-io-design.md \u00a716.5).

Mirrors test_cli_serve.py's shape: covers the CLI-argument-wiring/
config-error-exit-2/dependency-error-exit-2 surface that's cli.py's own
job here. setup_models() itself (the actual model pulling) is covered by
test_voice_io.py -- mocked here via mocker.patch("viva.cli.setup_models")
so no real faster-whisper/Piper import or download ever happens.

Every test that reaches Config.load() mocks viva.config.load_dotenv
(the established pattern -- test_cli_session.py's own comment: "don't
let a real .env override the missing var"). A real-world bug
(docs/system-design/16-phase-11-voice-io-design.md \u00a716.9): the first
version of this file skipped that on the tests asserting Config
*defaults* specifically, which happened to pass in an environment with
no real .env file, then failed against a real repo checkout whose
.env still had the pre-Phase-11-patch-10 STT_MODEL_SIZE=base -- the
real .env shadowed the coded default the test meant to exercise.
"""
from __future__ import annotations

from typer.testing import CliRunner

from viva.cli import app
from viva.voice_io import VoiceDependencyError

runner = CliRunner()


def test_voice_setup_help_lists_options():
    result = runner.invoke(
        app, ["voice", "setup", "--help"], env={"_TYPER_FORCE_DISABLE_TERMINAL": "1"}
    )

    assert result.exit_code == 0
    assert "--stt-model" in result.output
    assert "--tts-voice" in result.output


def test_voice_setup_config_error_exits_2(mocker, monkeypatch, tmp_path):
    mocker.patch("viva.config.load_dotenv")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["voice", "setup"])

    assert result.exit_code == 2


def test_voice_setup_calls_setup_models_with_config_defaults(mocker, monkeypatch, tmp_path):
    mocker.patch("viva.config.load_dotenv")
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "viva.db"))
    monkeypatch.setenv("VOICE_CACHE_DIR", str(tmp_path / "voice_models"))
    setup_mock = mocker.patch("viva.cli.setup_models")

    result = runner.invoke(app, ["voice", "setup"])

    assert result.exit_code == 0
    setup_mock.assert_called_once_with("small", "en_US-lessac-medium", str(tmp_path / "voice_models"))


def test_voice_setup_cli_flags_override_config_defaults(mocker, monkeypatch, tmp_path):
    mocker.patch("viva.config.load_dotenv")
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "viva.db"))
    setup_mock = mocker.patch("viva.cli.setup_models")

    result = runner.invoke(
        app, ["voice", "setup", "--stt-model", "small", "--tts-voice", "en_GB-alan-medium"]
    )

    assert result.exit_code == 0
    args, _kwargs = setup_mock.call_args
    assert args[0] == "small"
    assert args[1] == "en_GB-alan-medium"


def test_voice_setup_missing_extra_exits_2(mocker, monkeypatch, tmp_path):
    mocker.patch("viva.config.load_dotenv")
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "viva.db"))
    mocker.patch(
        "viva.cli.setup_models",
        side_effect=VoiceDependencyError('Run pip install -e ".[voice]"'),
    )

    result = runner.invoke(app, ["voice", "setup"])

    assert result.exit_code == 2
    assert "voice" in result.output.lower()


def test_voice_setup_missing_extra_message_is_not_mangled_by_rich_markup(mocker, monkeypatch, tmp_path):
    # Regression test for a real-world bug (docs/system-design/
    # 16-phase-11-voice-io-design.md §16.9): the VoiceDependencyError
    # text contains a literal "[voice]" (from `pip install -e ".[voice]"`
    # in the message itself), which Rich's markup parser was silently
    # swallowing as an unrecognized tag before this was escaped --
    # console.print(f"[red]{exc}[/red]") turned "...-e \".[voice]\" and..."
    # into "...-e \".\" and...", dropping the exact instruction the user
    # needed to fix the problem.
    mocker.patch("viva.config.load_dotenv")
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "viva.db"))
    mocker.patch(
        "viva.cli.setup_models",
        side_effect=VoiceDependencyError(
            'faster-whisper is not installed. Run pip install -e ".[voice]" and '
            "`viva voice setup` first."
        ),
    )

    result = runner.invoke(app, ["voice", "setup"])

    assert result.exit_code == 2
    assert 'pip install -e ".[voice]"' in result.output
