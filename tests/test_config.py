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
        "LINE_WINDOW_SIZE",
        "LINE_WINDOW_OVERLAP",
        "VECTOR_DB_PATH",
        "TOP_K_RETRIEVAL",
        "SESSION_DB_PATH",
        "AVG_TIME_PER_CATEGORY_SECONDS",
        "QUESTION_SIMILARITY_THRESHOLD",
        "EVAL_FLUSH_TIMEOUT_SECONDS",
        "REPORT_MAX_ITEMS_PER_SECTION",
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
    assert config.line_window_size == 60
    assert config.line_window_overlap == 15
    assert config.vector_db_path == "./data/chroma"
    assert config.top_k_retrieval == 5
    assert config.session_db_path == "./data/viva.db"
    assert config.avg_time_per_category_seconds == 180
    assert config.question_similarity_threshold == 0.90
    assert config.eval_flush_timeout_seconds == 60
    assert config.report_max_items_per_section == 10


def test_invalid_report_max_items_per_section_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("REPORT_MAX_ITEMS_PER_SECTION", "0")
    with pytest.raises(ConfigError, match="REPORT_MAX_ITEMS_PER_SECTION"):
        Config.load(env_file=None)


def test_report_max_items_per_section_accepts_a_custom_value(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("REPORT_MAX_ITEMS_PER_SECTION", "25")
    config = Config.load(env_file=None)
    assert config.report_max_items_per_section == 25


def test_invalid_eval_flush_timeout_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("EVAL_FLUSH_TIMEOUT_SECONDS", "0")
    with pytest.raises(ConfigError, match="EVAL_FLUSH_TIMEOUT_SECONDS"):
        Config.load(env_file=None)


def test_eval_flush_timeout_accepts_a_custom_value(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("EVAL_FLUSH_TIMEOUT_SECONDS", "15.5")
    config = Config.load(env_file=None)
    assert config.eval_flush_timeout_seconds == 15.5


def test_invalid_question_similarity_threshold_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("QUESTION_SIMILARITY_THRESHOLD", "1.5")
    with pytest.raises(ConfigError, match="QUESTION_SIMILARITY_THRESHOLD"):
        Config.load(env_file=None)


def test_zero_question_similarity_threshold_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("QUESTION_SIMILARITY_THRESHOLD", "0")
    with pytest.raises(ConfigError, match="QUESTION_SIMILARITY_THRESHOLD"):
        Config.load(env_file=None)


def test_invalid_avg_time_per_category_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("AVG_TIME_PER_CATEGORY_SECONDS", "0")
    with pytest.raises(ConfigError, match="AVG_TIME_PER_CATEGORY_SECONDS"):
        Config.load(env_file=None)


def test_empty_session_db_path_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("SESSION_DB_PATH", "   ")
    with pytest.raises(ConfigError, match="SESSION_DB_PATH"):
        Config.load(env_file=None)


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


def test_line_window_overlap_equal_to_size_raises(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("LINE_WINDOW_SIZE", "60")
    monkeypatch.setenv("LINE_WINDOW_OVERLAP", "60")
    with pytest.raises(ConfigError, match="LINE_WINDOW_OVERLAP"):
        Config.load(env_file=None)


def test_line_window_overlap_zero_allowed(monkeypatch):
    # 0 is a legitimate value (non-overlapping windows) -- unlike
    # LINE_WINDOW_SIZE, this must not raise.
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("LINE_WINDOW_OVERLAP", "0")
    config = Config.load(env_file=None)
    assert config.line_window_overlap == 0


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
