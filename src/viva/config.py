"""Environment-file configuration loading (FR28).

Phase 0 scope: only the handful of settings the walking skeleton actually
touches (LLM model, temperature, Ollama host, viva duration) are validated
here. Full validation of every tunable in README.md's configuration table
is Phase 1 work (docs/plan.md).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    llm_model: str
    embedding_model: str
    temperature: float
    ollama_host: str
    viva_duration_minutes: int

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

        duration_raw = os.getenv("VIVA_DURATION_MINUTES", "30")
        try:
            viva_duration_minutes = int(duration_raw)
        except ValueError as exc:
            raise ConfigError(
                f"VIVA_DURATION_MINUTES must be an integer, got {duration_raw!r}"
            ) from exc
        if viva_duration_minutes <= 0:
            raise ConfigError("VIVA_DURATION_MINUTES must be positive")

        return cls(
            llm_model=llm_model,
            embedding_model=embedding_model,
            temperature=temperature,
            ollama_host=ollama_host,
            viva_duration_minutes=viva_duration_minutes,
        )
