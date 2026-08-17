import pytest

from viva.config import Config, ConfigError


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "LLM_MODEL",
        "EMBEDDING_MODEL",
        "TEMPERATURE",
        "OLLAMA_HOST",
        "VIVA_DURATION_MINUTES",
    ):
        monkeypatch.delenv(key, raising=False)


def test_missing_llm_model_raises(monkeypatch):
    with pytest.raises(ConfigError, match="LLM_MODEL"):
        Config.load(env_file=None)


def test_defaults_applied(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    config = Config.load(env_file=None)
    assert config.embedding_model == "nomic-embed-text"
    assert config.ollama_host == "http://localhost:11434"
    assert config.temperature == 0.3
    assert config.viva_duration_minutes == 30


def test_invalid_temperature_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("TEMPERATURE", "not-a-number")
    with pytest.raises(ConfigError, match="TEMPERATURE"):
        Config.load(env_file=None)


def test_out_of_range_temperature_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("TEMPERATURE", "5")
    with pytest.raises(ConfigError, match="TEMPERATURE"):
        Config.load(env_file=None)


def test_invalid_duration_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("VIVA_DURATION_MINUTES", "-5")
    with pytest.raises(ConfigError, match="VIVA_DURATION_MINUTES"):
        Config.load(env_file=None)
