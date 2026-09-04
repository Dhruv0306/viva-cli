"""Tests for the FastAPI app (docs/plan.md Phase 10, docs/system-design/
15-phase-10-web-ui-design.md \u00a715.5).

Live-session routes (start/resume/state/answer) mock `SessionRegistry`
itself, the same "mock the thing this layer delegates to" pattern
`test_cli_session.py` uses for `Orchestrator` -- `SessionRegistry`'s own
thread-bridging behavior is covered by `test_web_registry.py`. list/
report/cleanup exercise a real `SessionStore`/`VectorStore` against a
tmp DB, following `test_cli_report.py`/`test_cli_cleanup.py`'s pattern,
since those routes' whole job is reading and formatting what's actually
persisted -- there's nothing worth mocking there either.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from viva.config import Config
from viva.orchestrator import (
    SessionAlreadyCompleteError,
    SessionNotFoundError,
    SessionNotResumableError,
)
from viva.questiongen.models import QuestionPlanItem
from viva.schemas import EvaluationRecord
from viva.storage import SessionStore
from viva.web.app import create_app


def _config(tmp_path):
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


class _FakeUI:
    def __init__(self, stage="awaiting_answer"):
        self.stage = stage
        self.answers: list[str] = []

    def snapshot(self) -> dict:
        return {
            "session_id": "sess-fixed-id", "stage": self.stage, "detail": None,
            "question_text": "Why is this a dataclass?", "category": "design",
            "question_number": 1, "error_message": None, "summary": None,
            "remaining_seconds": 120.0,
        }

    def submit_answer(self, text: str) -> bool:
        if self.stage != "awaiting_answer":
            return False
        self.answers.append(text)
        return True


class _FakeRegistry:
    """Stand-in for `SessionRegistry`, swapped in via `mocker.patch`."""

    def __init__(self, config):
        self.config = config
        self.sessions: dict[str, _FakeUI] = {}
        self.start_exc = None
        self.resume_exc = None
        self.shutdown_called = False

    def start_session(self, repo_url, branch, duration_minutes, session_name):
        if self.start_exc:
            raise self.start_exc
        ui = _FakeUI()
        self.sessions["sess-fixed-id"] = ui
        return "sess-fixed-id"

    def resume_session(self, session_id):
        if self.resume_exc:
            raise self.resume_exc
        ui = _FakeUI()
        self.sessions[session_id] = ui
        return ui

    def get(self, session_id):
        return self.sessions.get(session_id)

    def shutdown(self):
        self.shutdown_called = True


def _client_with_fake_registry(mocker, tmp_path) -> tuple[TestClient, _FakeRegistry]:
    fake = _FakeRegistry(_config(tmp_path))
    mocker.patch("viva.web.app.SessionRegistry", return_value=fake)
    app = create_app(_config(tmp_path))
    return TestClient(app), fake


# -- POST /api/sessions ------------------------------------------------------

def test_start_session_returns_session_id(mocker, tmp_path):
    client, _fake = _client_with_fake_registry(mocker, tmp_path)

    response = client.post("/api/sessions", json={"repo_url": "https://github.com/owner/repo"})

    assert response.status_code == 200
    assert response.json() == {"session_id": "sess-fixed-id"}


def test_start_session_failure_returns_500(mocker, tmp_path):
    client, fake = _client_with_fake_registry(mocker, tmp_path)
    fake.start_exc = RuntimeError("SessionStore is unavailable")

    response = client.post("/api/sessions", json={"repo_url": "https://github.com/owner/repo"})

    assert response.status_code == 500


# -- POST /api/sessions/{id}/resume ------------------------------------------

def test_resume_not_found_returns_404(mocker, tmp_path):
    client, fake = _client_with_fake_registry(mocker, tmp_path)
    fake.resume_exc = SessionNotFoundError("No session found with id 'x'.")

    response = client.post("/api/sessions/x/resume")

    assert response.status_code == 404


def test_resume_already_complete_returns_409(mocker, tmp_path):
    client, fake = _client_with_fake_registry(mocker, tmp_path)
    fake.resume_exc = SessionAlreadyCompleteError("Session x is already complete.")

    response = client.post("/api/sessions/x/resume")

    assert response.status_code == 409


def test_resume_not_resumable_returns_409(mocker, tmp_path):
    client, fake = _client_with_fake_registry(mocker, tmp_path)
    fake.resume_exc = SessionNotResumableError("Session x was interrupted during setup.")

    response = client.post("/api/sessions/x/resume")

    assert response.status_code == 409


def test_resume_success_returns_session_id(mocker, tmp_path):
    client, _fake = _client_with_fake_registry(mocker, tmp_path)

    response = client.post("/api/sessions/sess-fixed-id/resume")

    assert response.status_code == 200
    assert response.json() == {"session_id": "sess-fixed-id"}


# -- GET /api/sessions/{id}/state --------------------------------------------

def test_state_unknown_session_returns_404(mocker, tmp_path):
    client, _fake = _client_with_fake_registry(mocker, tmp_path)

    response = client.get("/api/sessions/never-started/state")

    assert response.status_code == 404


def test_state_returns_snapshot(mocker, tmp_path):
    client, fake = _client_with_fake_registry(mocker, tmp_path)
    fake.sessions["sess-fixed-id"] = _FakeUI()

    response = client.get("/api/sessions/sess-fixed-id/state")

    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "awaiting_answer"
    assert body["question_text"] == "Why is this a dataclass?"


# -- POST /api/sessions/{id}/answer ------------------------------------------

def test_submit_answer_unknown_session_returns_404(mocker, tmp_path):
    client, _fake = _client_with_fake_registry(mocker, tmp_path)

    response = client.post("/api/sessions/never-started/answer", json={"text": "hi"})

    assert response.status_code == 404


def test_submit_answer_success(mocker, tmp_path):
    client, fake = _client_with_fake_registry(mocker, tmp_path)
    ui = _FakeUI()
    fake.sessions["sess-fixed-id"] = ui

    response = client.post("/api/sessions/sess-fixed-id/answer", json={"text": "my answer"})

    assert response.status_code == 200
    assert ui.answers == ["my answer"]


def test_submit_answer_when_not_awaiting_returns_409(mocker, tmp_path):
    client, fake = _client_with_fake_registry(mocker, tmp_path)
    fake.sessions["sess-fixed-id"] = _FakeUI(stage="working")

    response = client.post("/api/sessions/sess-fixed-id/answer", json={"text": "too late"})

    assert response.status_code == 409


# -- GET /api/sessions (real SessionStore) -----------------------------------

def test_list_sessions_reads_real_store(mocker, tmp_path):
    config = _config(tmp_path)
    store = SessionStore(config.session_db_path)
    store.create_session("sess1", "https://github.com/o/r", "main", "demo", 1800)
    store.close()
    mocker.patch("viva.web.app.SessionRegistry", return_value=_FakeRegistry(config))
    client = TestClient(create_app(config))

    response = client.get("/api/sessions")

    assert response.status_code == 200
    ids = [s["session_id"] for s in response.json()]
    assert ids == ["sess1"]


def test_list_sessions_resumable_field_matches_orchestrator_validation(mocker, tmp_path):
    # Regression test: the sessions list used to compute "can this be
    # resumed" purely on the frontend (status != COMPLETE/FAILED), which
    # didn't match Orchestrator.resume()'s own, stricter validation --
    # a session interrupted mid-setup (e.g. ANALYZING) showed a "Resume"
    # button that could only ever 409 (confirmed against a real
    # `viva serve` run's log, this session). `resumable` now comes
    # straight from orchestrator.is_resumable(), the same check
    # resume() itself applies.
    config = _config(tmp_path)
    store = SessionStore(config.session_db_path)
    store.create_session("mid-setup", "https://github.com/o/r", None, None, 1800)
    store.update_status("mid-setup", "ANALYZING")
    store.create_session("live", "https://github.com/o/r", None, None, 1800)
    store.update_status("live", "IN_PROGRESS")
    store.close()
    mocker.patch("viva.web.app.SessionRegistry", return_value=_FakeRegistry(config))
    client = TestClient(create_app(config))

    response = client.get("/api/sessions")

    resumable_by_id = {s["session_id"]: s["resumable"] for s in response.json()}
    assert resumable_by_id == {"mid-setup": False, "live": True}


# -- GET /api/sessions/{id}/report (real SessionStore/ReportBuilder) --------

def _seed_completed_session(db_path: str) -> None:
    store = SessionStore(db_path)
    store.create_session("sess1", "https://github.com/o/r", "main", "demo", 1800)
    store.set_pipeline_artifacts(
        "sess1", repo_slug="o/r", commit_sha="abc123def456",
        collection_name="o--r-abc123def456", profile_path="/tmp/p.json",
    )
    store.save_plan("sess1", [QuestionPlanItem(id="q1", category="architecture", target_module=None)])
    store.record_question_asked("sess1", "q1", "How does the Orchestrator work?", [])
    store.record_answer("sess1", "q1", "It mediates every component.")
    record = EvaluationRecord(
        classification="correct", summary="Correctly described the mediator role.",
        cited_file="src/viva/orchestrator.py",
        did_well=["Named the Orchestrator's mediator role correctly."],
        missed=[], did_wrong=[], improvement="None needed.", needs_review=False,
    )
    store.set_eval_complete("sess1", "q1", record.model_dump_json(), needs_review=False)
    store.update_status("sess1", "COMPLETE")
    store.close()


def _app_client(mocker, tmp_path) -> TestClient:
    config = _config(tmp_path)
    mocker.patch("viva.web.app.SessionRegistry", return_value=_FakeRegistry(config))
    return TestClient(create_app(config))


def test_report_not_found_returns_404(mocker, tmp_path):
    client = _app_client(mocker, tmp_path)

    response = client.get("/api/sessions/does-not-exist/report")

    assert response.status_code == 404


def test_report_incomplete_without_allow_partial_returns_409(mocker, tmp_path):
    config = _config(tmp_path)
    store = SessionStore(config.session_db_path)
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.update_status("sess1", "IN_PROGRESS")
    store.close()
    client = _app_client(mocker, tmp_path)

    response = client.get("/api/sessions/sess1/report")

    assert response.status_code == 409


def test_report_bad_format_returns_400(mocker, tmp_path):
    _seed_completed_session(_config(tmp_path).session_db_path)
    client = _app_client(mocker, tmp_path)

    response = client.get("/api/sessions/sess1/report?format=xml")

    assert response.status_code == 400


def test_report_markdown_default(mocker, tmp_path):
    config = _config(tmp_path)
    _seed_completed_session(config.session_db_path)
    mocker.patch("viva.web.app.SessionRegistry", return_value=_FakeRegistry(config))
    client = TestClient(create_app(config))

    response = client.get("/api/sessions/sess1/report")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "# Viva Report" in response.text


def test_report_json_format(mocker, tmp_path):
    config = _config(tmp_path)
    _seed_completed_session(config.session_db_path)
    mocker.patch("viva.web.app.SessionRegistry", return_value=_FakeRegistry(config))
    client = TestClient(create_app(config))

    response = client.get("/api/sessions/sess1/report?format=json")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["session_id"] == "sess1"


# -- POST /api/cleanup (real SessionStore/VectorStore) -----------------------

def test_cleanup_invalid_older_than_returns_400(mocker, tmp_path):
    client = _app_client(mocker, tmp_path)

    response = client.post("/api/cleanup", json={"older_than": 0})

    assert response.status_code == 400


def test_cleanup_removes_nothing_on_empty_store(mocker, tmp_path):
    client = _app_client(mocker, tmp_path)

    response = client.post("/api/cleanup", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["sessions_removed"] == []
    assert body["is_empty"] is True


def test_cleanup_purge_all_removes_existing_session(mocker, tmp_path):
    config = _config(tmp_path)
    store = SessionStore(config.session_db_path)
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.close()
    client = _app_client(mocker, tmp_path)

    response = client.post("/api/cleanup", json={"all": True})

    assert response.status_code == 200
    assert response.json()["sessions_removed"] == ["sess1"]


# -- static frontend -----------------------------------------------------------

def test_root_serves_index_html(mocker, tmp_path):
    client = _app_client(mocker, tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert "viva-web" in response.text


def test_static_assets_referenced_by_index_html_are_served(mocker, tmp_path):
    # Regression test for a real bug: app.py used to mount StaticFiles at
    # "/" (html=True), which serves index.html at "/" but does *not*
    # register a /static prefix -- so index.html's own <link>/<script>
    # references to /static/style.css and /static/app.js 404'd, even
    # though GET / itself returned 200. Confirmed against a real
    # `viva serve` run (this session): 200 on GET /, 404 on both asset
    # requests.
    client = _app_client(mocker, tmp_path)

    css = client.get("/static/style.css")
    js = client.get("/static/app.js")

    assert css.status_code == 200
    assert js.status_code == 200


def test_favicon_served_at_root_favicon_ico(mocker, tmp_path):
    # Browsers request /favicon.ico directly, independent of index.html's
    # <link rel="icon"> tag -- without this route it 404s even though
    # GET / itself works fine (confirmed in a real `viva serve` run).
    client = _app_client(mocker, tmp_path)

    response = client.get("/favicon.ico")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in response.content
