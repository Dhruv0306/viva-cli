"""Tests for `SessionRegistry` (docs/plan.md Phase 10, docs/system-design/
15-phase-10-web-ui-design.md \u00a715.3/\u00a715.4).

Mocks `viva.web.registry.Orchestrator` itself, the same pattern
`test_cli_session.py` uses for `viva.cli.Orchestrator` -- the pipeline
Orchestrator drives is covered by `test_orchestrator.py`; these tests
exercise the thread-spawn/session_id-wait/error-propagation plumbing
that's actually `SessionRegistry`'s own job, without a real Ollama call
anywhere in the loop.
"""
from __future__ import annotations

import pytest

from viva.orchestrator import SessionNotFoundError
from viva.web.registry import SessionRegistry
from viva.web.web_session_ui import STAGE_ERROR


class _FakeOrchestrator:
    """Stand-in for `Orchestrator`, capturing the `ui` it's constructed
    with so a test can drive it the way the real Orchestrator would."""

    def __init__(self, config, session_store, ui, **_kw):
        self.ui = ui

    def start(self, repo_url, branch=None, duration_minutes=None, session_name=None):
        self.ui.session_started("sess-fixed-id")
        return "sess-fixed-id"

    def resume(self, session_id):
        self.ui.session_started(session_id)


class _FailAfterIdOrchestrator(_FakeOrchestrator):
    """Simulates a clone/LLM failure that happens *after* session_id is
    already assigned -- e.g. CloneError inside _run_setup_pipeline,
    which Orchestrator.start() itself only raises once the session row
    (and session_started() call) already exist."""

    def start(self, repo_url, branch=None, duration_minutes=None, session_name=None):
        self.ui.session_started("sess-fixed-id")
        raise RuntimeError("Clone failed: repository not found")


class _NotFoundOrchestrator:
    """Simulates Orchestrator.resume()'s own not-found check, which
    fires *before* session_started() is ever called."""

    def __init__(self, config, session_store, ui, **_kw):
        self.ui = ui

    def resume(self, session_id):
        raise SessionNotFoundError(f"No session found with id {session_id!r}.")


def _config(tmp_path):
    from viva.config import Config

    return Config(
        llm_model="gemma4:e4b", embedding_model="nomic-embed-text", temperature=0.3,
        ollama_host="http://localhost:11434", viva_duration_minutes=30, max_questions=20,
        max_followup_depth=2, session_retention_days=30, max_files=200,
        test_file_quota_pct=30, github_token=None, map_reduce_batch_size=10,
        max_reduce_context_tokens=None, line_window_size=60, line_window_overlap=10,
        vector_db_path=str(tmp_path / "chroma"), top_k_retrieval=8,
        session_db_path=str(tmp_path / "viva.db"), avg_time_per_category_seconds=90,
        question_similarity_threshold=0.85, eval_flush_timeout_seconds=5.0,
        report_max_items_per_section=10,
    )


def test_start_session_returns_id_reported_via_session_started(mocker, tmp_path):
    mocker.patch("viva.web.registry.Orchestrator", _FakeOrchestrator)
    registry = SessionRegistry(_config(tmp_path))

    session_id = registry.start_session(
        "https://github.com/owner/repo", branch=None, duration_minutes=None, session_name=None,
    )

    assert session_id == "sess-fixed-id"
    assert registry.get("sess-fixed-id") is not None


def test_start_session_registers_even_if_orchestrator_fails_after_id_assigned(mocker, tmp_path):
    mocker.patch("viva.web.registry.Orchestrator", _FailAfterIdOrchestrator)
    registry = SessionRegistry(_config(tmp_path))

    # Mirrors the CLI contract's "prints session_id immediately" guarantee
    # (06-cli-contract-and-profile-scaling.md \u00a76.1): a failure after the
    # id exists doesn't prevent it from being returned/registered -- it's
    # surfaced via the ui's state instead (design doc \u00a715.3).
    session_id = registry.start_session(
        "https://github.com/owner/repo", branch=None, duration_minutes=None, session_name=None,
    )

    assert session_id == "sess-fixed-id"
    ui = registry.get("sess-fixed-id")
    assert ui is not None

    # The background thread's failure is asynchronous -- give it a beat
    # to reach the except block and call ui.error().
    for live in registry._sessions.values():  # noqa: SLF001 - test-only introspection, same pattern test_cli_cleanup.py uses on SessionStore internals
        live.thread.join(timeout=2.0)
    assert ui.snapshot()["stage"] == STAGE_ERROR


def test_resume_session_raises_session_not_found_before_registering(mocker, tmp_path):
    mocker.patch("viva.web.registry.Orchestrator", _NotFoundOrchestrator)
    registry = SessionRegistry(_config(tmp_path))

    with pytest.raises(SessionNotFoundError):
        registry.resume_session("nonexistent")

    assert registry.get("nonexistent") is None


def test_resume_session_success_registers_the_session(mocker, tmp_path):
    mocker.patch("viva.web.registry.Orchestrator", _FakeOrchestrator)
    registry = SessionRegistry(_config(tmp_path))

    ui = registry.resume_session("sess-fixed-id")

    assert ui.session_id == "sess-fixed-id"
    assert registry.get("sess-fixed-id") is ui


def test_get_returns_none_for_unknown_session(tmp_path):
    registry = SessionRegistry(_config(tmp_path))

    assert registry.get("never-started") is None


def test_shutdown_requests_shutdown_on_every_live_session(mocker, tmp_path):
    mocker.patch("viva.web.registry.Orchestrator", _FakeOrchestrator)
    registry = SessionRegistry(_config(tmp_path))
    registry.start_session("https://github.com/owner/repo", None, None, None)

    registry.shutdown()

    live = next(iter(registry._sessions.values()))  # noqa: SLF001 - test-only introspection
    assert live.ui._shutdown.is_set()  # noqa: SLF001 - test-only introspection
