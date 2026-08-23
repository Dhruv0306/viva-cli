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
        "MAX_QUESTIONS",
        "MAX_FOLLOWUP_DEPTH",
        "SESSION_RETENTION_DAYS",
        "MAX_FILES",
        "TEST_FILE_QUOTA_PCT",
        "GITHUB_TOKEN",
        "MAP_REDUCE_BATCH_SIZE",
        "MAX_REDUCE_CONTEXT_TOKENS",
        "VECTOR_DB_PATH",
        "TOP_K_RETRIEVAL",
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
    assert config.max_questions == 8
    assert config.max_followup_depth == 1
    assert config.session_retention_days == 7
    assert config.max_files == 500
    assert config.test_file_quota_pct == 10
    assert config.github_token is None
    assert config.map_reduce_batch_size == 8
    assert config.max_reduce_context_tokens is None
    assert config.vector_db_path == "./data/chroma"
    assert config.top_k_retrieval == 5


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


def test_max_followup_depth_allows_zero(monkeypatch):
    # 0 is a legitimate value (no follow-ups at all) -- unlike the strictly
    # positive fields, this must not raise.
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("MAX_FOLLOWUP_DEPTH", "0")
    config = Config.load(env_file=None)
    assert config.max_followup_depth == 0


def test_negative_max_followup_depth_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("MAX_FOLLOWUP_DEPTH", "-1")
    with pytest.raises(ConfigError, match="MAX_FOLLOWUP_DEPTH"):
        Config.load(env_file=None)


def test_invalid_max_questions_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("MAX_QUESTIONS", "0")
    with pytest.raises(ConfigError, match="MAX_QUESTIONS"):
        Config.load(env_file=None)


def test_invalid_max_files_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("MAX_FILES", "not-a-number")
    with pytest.raises(ConfigError, match="MAX_FILES"):
        Config.load(env_file=None)


def test_test_file_quota_pct_over_100_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("TEST_FILE_QUOTA_PCT", "150")
    with pytest.raises(ConfigError, match="TEST_FILE_QUOTA_PCT"):
        Config.load(env_file=None)


def test_test_file_quota_pct_zero_allowed(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("TEST_FILE_QUOTA_PCT", "0")
    config = Config.load(env_file=None)
    assert config.test_file_quota_pct == 0


def test_github_token_blank_is_none(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("GITHUB_TOKEN", "   ")
    config = Config.load(env_file=None)
    assert config.github_token is None


def test_github_token_set_is_kept(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_example123")
    config = Config.load(env_file=None)
    assert config.github_token == "ghp_example123"


def test_max_reduce_context_tokens_unset_is_none(monkeypatch):
    # Deliberately left unset in .env.example -- Phase 3 computes this at
    # runtime as a fraction of the model's context window, so None must be
    # accepted rather than required.
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    config = Config.load(env_file=None)
    assert config.max_reduce_context_tokens is None


def test_max_reduce_context_tokens_invalid_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("MAX_REDUCE_CONTEXT_TOKENS", "-100")
    with pytest.raises(ConfigError, match="MAX_REDUCE_CONTEXT_TOKENS"):
        Config.load(env_file=None)


def test_invalid_top_k_retrieval_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("TOP_K_RETRIEVAL", "0")
    with pytest.raises(ConfigError, match="TOP_K_RETRIEVAL"):
        Config.load(env_file=None)


def test_empty_vector_db_path_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("VECTOR_DB_PATH", "   ")
    with pytest.raises(ConfigError, match="VECTOR_DB_PATH"):
        Config.load(env_file=None)
