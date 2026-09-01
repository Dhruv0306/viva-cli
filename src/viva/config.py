"""Environment-file configuration loading (FR28).

Phase 0 scope was only the handful of settings the walking skeleton actually
touched (LLM model, temperature, Ollama host, viva duration). Phase 1 (see
docs/plan.md) extends validation to every tunable in `.env.example` /
README.md's configuration table, so a bad value fails fast at startup rather
than surfacing as a confusing error deep in a later phase's pipeline code.

Two fields are deliberately *not* strictly validated here:

- `MAX_REDUCE_CONTEXT_TOKENS` is left unset in `.env.example` on purpose --
  it's computed as a fraction of `LLM_MODEL`'s context window at runtime by
  Phase 3 (docs/system-design/06-cli-contract-and-profile-scaling.md §6.2),
  not hardcoded. Config only checks it's a positive int *if* the user has
  set it; `None` is a valid, expected value.
- `GITHUB_TOKEN` is optional (public repos don't need one) and is not
  format-validated -- GitHub's token formats change over time, and the
  first real use of it (Phase 2 ingestion) will surface an invalid token
  clearly enough via the GitHub API's own error.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _get_positive_int(name: str, default: str) -> int:
    raw = os.getenv(name, default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}")
    return value


def _get_non_negative_int(name: str, default: str) -> int:
    raw = os.getenv(name, default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise ConfigError(f"{name} must be zero or positive, got {value}")
    return value


def _get_unit_interval_float(name: str, default: str) -> float:
    """Parses a float in (0.0, 1.0] -- used for similarity thresholds."""
    raw = os.getenv(name, default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not (0.0 < value <= 1.0):
        raise ConfigError(f"{name} must be between 0 and 1 (exclusive of 0), got {value}")
    return value


def _get_positive_float(name: str, default: str) -> float:
    raw = os.getenv(name, default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}")
    return value


def _get_optional_positive_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer if set, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive if set, got {value}")
    return value


@dataclass(frozen=True)
class Config:
    # --- LLM / Embeddings (Ollama) ---
    llm_model: str
    embedding_model: str
    temperature: float
    ollama_host: str

    # --- Session ---
    viva_duration_minutes: int
    max_questions: int
    max_followup_depth: int
    session_retention_days: int

    # --- Ingestion / Sampling ---
    max_files: int
    test_file_quota_pct: int
    github_token: str | None

    # --- Analysis ---
    map_reduce_batch_size: int
    max_reduce_context_tokens: int | None
    line_window_size: int
    line_window_overlap: int

    # --- RAG ---
    vector_db_path: str
    top_k_retrieval: int

    # --- Session persistence / loop (Phase 6, docs/design.md §8) ---
    session_db_path: str
    # NOTE: currently unused by the Orchestrator's question-selection logic
    # (see orchestrator.py's _select_next_item docstring) -- an earlier
    # version used this to gate a one-time "collapse to breadth" decision
    # made at session start, which proved harmful (permanently capped
    # short sessions at exactly `categories` questions regardless of
    # actual pacing). Left defined rather than removed, to avoid another
    # config-shape ripple across the test suite; kept here as a flag for
    # anyone who goes looking for what reads it and finds nothing.
    avg_time_per_category_seconds: int
    question_similarity_threshold: float

    # --- Evaluation (Phase 7, docs/system-design/12-phase-7-evaluator-design.md) ---
    # Bound on how long FINALIZING_EVALS waits for the Evaluator's
    # background worker thread to drain before giving up and marking
    # whatever's left needs_review (§12.6) -- session end must never hang
    # indefinitely on one stuck model call.
    eval_flush_timeout_seconds: float

    @classmethod
    def load(cls, env_file: str | None = ".env") -> "Config":
        """Load configuration from environment variables.

        Reads a `.env` file (if present) via python-dotenv, then reads from
        the process environment so real environment variables can still
        override `.env` for CI/deployment use cases.
        """
        if env_file:
            load_dotenv(env_file)

        llm_model = os.getenv("LLM_MODEL", "").strip()
        if not llm_model:
            raise ConfigError(
                "LLM_MODEL is not set. Copy .env.example to .env and set "
                "LLM_MODEL to an Ollama model tag you've pulled, e.g. "
                "`ollama pull qwen2.5-coder:7b`."
            )

        embedding_model = os.getenv("EMBEDDING_MODEL", "nomic-embed-text").strip()
        ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434").strip()

        temperature_raw = os.getenv("TEMPERATURE", "0.3")
        try:
            temperature = float(temperature_raw)
        except ValueError as exc:
            raise ConfigError(
                f"TEMPERATURE must be a number, got {temperature_raw!r}"
            ) from exc
        if not 0.0 <= temperature <= 2.0:
            raise ConfigError(
                f"TEMPERATURE must be between 0.0 and 2.0, got {temperature}"
            )

        viva_duration_minutes = _get_positive_int("VIVA_DURATION_MINUTES", "30")
        max_questions = _get_positive_int("MAX_QUESTIONS", "8")
        # 0 is a legitimate choice here (no follow-ups at all), so this is
        # non-negative rather than strictly positive like the others.
        max_followup_depth = _get_non_negative_int("MAX_FOLLOWUP_DEPTH", "1")
        session_retention_days = _get_positive_int("SESSION_RETENTION_DAYS", "7")

        max_files = _get_positive_int("MAX_FILES", "500")

        test_file_quota_pct = _get_non_negative_int("TEST_FILE_QUOTA_PCT", "10")
        if test_file_quota_pct > 100:
            raise ConfigError(
                f"TEST_FILE_QUOTA_PCT must be between 0 and 100, got {test_file_quota_pct}"
            )

        github_token = os.getenv("GITHUB_TOKEN", "").strip() or None

        map_reduce_batch_size = _get_positive_int("MAP_REDUCE_BATCH_SIZE", "8")
        max_reduce_context_tokens = _get_optional_positive_int("MAX_REDUCE_CONTEXT_TOKENS")

        line_window_size = _get_positive_int("LINE_WINDOW_SIZE", "60")
        line_window_overlap = _get_non_negative_int("LINE_WINDOW_OVERLAP", "15")
        if line_window_overlap >= line_window_size:
            raise ConfigError(
                "LINE_WINDOW_OVERLAP must be smaller than LINE_WINDOW_SIZE, got "
                f"overlap={line_window_overlap} size={line_window_size}"
            )

        vector_db_path = os.getenv("VECTOR_DB_PATH", "./data/chroma").strip()
        if not vector_db_path:
            raise ConfigError("VECTOR_DB_PATH must not be empty if set")

        top_k_retrieval = _get_positive_int("TOP_K_RETRIEVAL", "5")

        session_db_path = os.getenv("SESSION_DB_PATH", "./data/viva.db").strip()
        if not session_db_path:
            raise ConfigError("SESSION_DB_PATH must not be empty if set")

        # Used by the Orchestrator's time-budget collapse check
        # (docs/design.md §7: "remaining_time / avg_time_per_remaining_category")
        # -- a tunable estimate rather than a hardcoded guess, per FR28, since
        # actual answer pacing varies a lot by person and by repo complexity.
        # NOTE: currently unused by selection logic -- see config.py's field
        # comment and docs/system-design/11-phase-6-session-loop-design.md §11.9.
        avg_time_per_category_seconds = _get_positive_int(
            "AVG_TIME_PER_CATEGORY_SECONDS", "180"
        )

        # Cosine-similarity threshold (embedding space) above which a
        # freshly generated question is treated as a likely duplicate of
        # one already asked this session -- FR15's third and most accurate
        # duplicate-avoidance layer (docs/system-design/
        # 11-phase-6-session-loop-design.md §11.9). A tunable per FR28
        # rather than a hardcoded guess, since it depends on the actual
        # embedding model in use and hasn't been empirically calibrated.
        question_similarity_threshold = _get_unit_interval_float(
            "QUESTION_SIMILARITY_THRESHOLD", "0.90"
        )

        # docs/system-design/12-phase-7-evaluator-design.md §12.6:
        # FINALIZING_EVALS's bound on draining the Evaluator's background
        # worker before giving up on whatever's left.
        eval_flush_timeout_seconds = _get_positive_float("EVAL_FLUSH_TIMEOUT_SECONDS", "60")

        return cls(
            llm_model=llm_model,
            embedding_model=embedding_model,
            temperature=temperature,
            ollama_host=ollama_host,
            viva_duration_minutes=viva_duration_minutes,
            max_questions=max_questions,
            max_followup_depth=max_followup_depth,
            session_retention_days=session_retention_days,
            max_files=max_files,
            test_file_quota_pct=test_file_quota_pct,
            github_token=github_token,
            map_reduce_batch_size=map_reduce_batch_size,
            max_reduce_context_tokens=max_reduce_context_tokens,
            line_window_size=line_window_size,
            line_window_overlap=line_window_overlap,
            vector_db_path=vector_db_path,
            top_k_retrieval=top_k_retrieval,
            session_db_path=session_db_path,
            avg_time_per_category_seconds=avg_time_per_category_seconds,
            question_similarity_threshold=question_similarity_threshold,
            eval_flush_timeout_seconds=eval_flush_timeout_seconds,
        )
