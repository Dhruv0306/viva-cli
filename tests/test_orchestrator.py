from __future__ import annotations

from pathlib import Path

import pytest

import viva.orchestrator as orchestrator_module
from viva.analyzer.models import AnalysisResult, AnalysisStats, ModuleSummary
from viva.classification import ClassificationProvider
from viva.config import Config
from viva.indexer.models import IndexResult, IndexStats
from viva.ingest.models import ExclusionStats, IngestResult, SampledFile
from viva.orchestrator import (
    Orchestrator,
    SessionAlreadyCompleteError,
    SessionNotFoundError,
    SessionNotResumableError,
)
from viva.questiongen.models import GeneratedQuestion, QuestionPlanItem
from viva.storage import SessionStore
from viva.storage.session_store import SKIPPED_TIME_COLLAPSE


class FakeSessionUI:
    """Scripted stand-in for `session_ui.SessionUI` -- no real terminal,
    no threads, so the loop can be tested deterministically (see
    `session_ui.py`'s docstring on why the interface exists)."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.events: list[tuple] = []
        self.summary = None

    def session_started(self, session_id):
        self.events.append(("session_started", session_id))

    def stage_started(self, stage):
        self.events.append(("stage_started", stage))

    def stage_completed(self, stage, detail):
        self.events.append(("stage_completed", stage))

    def ask_question(self, question_text, category, question_number):
        self.events.append(("ask", question_number, category))

    def read_answer(self, timer):
        return self._answers.pop(0)

    def time_expired(self):
        self.events.append(("time_expired",))

    def session_complete(self, summary):
        self.summary = summary
        self.events.append(("complete", summary.status))

    def error(self, message):
        self.events.append(("error", message))


def _config(tmp_path, **overrides) -> Config:
    values = dict(
        llm_model="test-model", embedding_model="nomic-embed-text", temperature=0.3,
        ollama_host="http://localhost:11434", viva_duration_minutes=30, max_questions=8,
        max_followup_depth=1, session_retention_days=7, max_files=500, test_file_quota_pct=10,
        github_token=None, map_reduce_batch_size=8, max_reduce_context_tokens=100_000,
        line_window_size=60, line_window_overlap=15, vector_db_path="./data/chroma",
        top_k_retrieval=5, session_db_path=str(tmp_path / "viva.db"),
        avg_time_per_category_seconds=1,
    )
    values.update(overrides)
    return Config(**values)


def _fake_ingest_result() -> IngestResult:
    return IngestResult(
        repo_url="https://github.com/owner/repo",
        repo_slug="owner/repo",
        commit_sha="abc123",
        branch="main",
        local_path=Path("/tmp/clone"),
        files_total=2,
        files_analyzed=2,
        sampled_files=[SampledFile(path="a.py", size_bytes=10, module="")],
        excluded_notable=[],
        sampling_note="analyzed 2/2",
        detected_stack=["python"],
        exclusion_stats=ExclusionStats(),
    )


def _fake_analysis_result() -> AnalysisResult:
    return AnalysisResult(
        architecture_summary="A small project.",
        modules=[ModuleSummary(module="core", summary="core module", file_count=1)],
        entry_points=["a.py"],
        test_coverage_present=True,
        analysis_stats=AnalysisStats(files_analyzed=2, ast_parsed=2),
    )


def _fake_plan() -> list[QuestionPlanItem]:
    return [
        QuestionPlanItem(id="q1", category="architecture", target_module=None),
        QuestionPlanItem(id="q2", category="implementation_detail", target_module="core"),
    ]


def _patch_pipeline(monkeypatch, plan=None, grounded=True):
    monkeypatch.setattr(orchestrator_module, "ingest_repo", lambda *a, **kw: _fake_ingest_result())
    monkeypatch.setattr(orchestrator_module, "analyze_repo", lambda *a, **kw: _fake_analysis_result())
    monkeypatch.setattr(
        orchestrator_module, "index_repo",
        lambda *a, **kw: IndexResult(collection_name="owner--repo-abc123",
                                      stats=IndexStats(files_processed=2, chunks_built=4)),
    )
    monkeypatch.setattr(
        orchestrator_module, "build_coverage_plan", lambda *a, **kw: plan or _fake_plan()
    )

    def fake_generate_question(plan_item, *a, **kw):
        if not grounded:
            return None
        return GeneratedQuestion(
            plan_item=plan_item,
            question_text=f"Question about {plan_item.category}?",
            grounding_chunk_ids=["chunk1"],
        )

    monkeypatch.setattr(orchestrator_module, "generate_question", fake_generate_question)


def _make_orchestrator(tmp_path, config, ui):
    store = SessionStore(str(tmp_path / "viva.db"))
    orch = Orchestrator(
        config=config, session_store=store, ui=ui,
        llm_client=object(), embedding_client=object(), vector_store=object(),
    )
    return orch, store


def test_start_runs_full_pipeline_to_complete(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _patch_pipeline(monkeypatch)
    ui = FakeSessionUI(answers=["answer one", "answer two"])
    orch, store = _make_orchestrator(tmp_path, config, ui)

    session_id = orch.start("https://github.com/owner/repo", branch="main")

    record = store.get_session(session_id)
    assert record.status == "COMPLETE"
    assert record.repo_slug == "owner/repo"
    assert record.commit_sha == "abc123"
    assert ui.summary.questions_answered == 2
    assert ui.summary.questions_asked == 2
    assert ("session_started", session_id) in ui.events


def test_start_persists_profile_for_resume(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _patch_pipeline(monkeypatch)
    ui = FakeSessionUI(answers=["a1", "a2"])
    orch, store = _make_orchestrator(tmp_path, config, ui)

    session_id = orch.start("https://github.com/owner/repo")

    record = store.get_session(session_id)
    assert record.profile_path is not None
    assert Path(record.profile_path).exists()


def test_start_marks_session_failed_on_pipeline_error(tmp_path, monkeypatch):
    config = _config(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("clone exploded")

    monkeypatch.setattr(orchestrator_module, "ingest_repo", boom)
    ui = FakeSessionUI(answers=[])
    orch, store = _make_orchestrator(tmp_path, config, ui)

    with pytest.raises(RuntimeError, match="clone exploded"):
        orch.start("https://github.com/owner/repo")

    session_id = store.list_sessions()[0].session_id
    record = store.get_session(session_id)
    assert record.status == "FAILED"
    assert "clone exploded" in record.error_message


def test_ungrounded_item_is_skipped_not_asked(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _patch_pipeline(monkeypatch, grounded=False)
    ui = FakeSessionUI(answers=[])
    orch, store = _make_orchestrator(tmp_path, config, ui)

    session_id = orch.start("https://github.com/owner/repo")

    record = store.get_session(session_id)
    assert record.status == "COMPLETE"
    assert ui.summary.questions_asked == 0
    assert ui.summary.questions_skipped == 2


def test_resume_raises_for_unknown_session(tmp_path):
    config = _config(tmp_path)
    ui = FakeSessionUI(answers=[])
    orch, _store = _make_orchestrator(tmp_path, config, ui)

    with pytest.raises(SessionNotFoundError):
        orch.resume("does-not-exist")


def test_resume_raises_for_already_complete_session(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _patch_pipeline(monkeypatch)
    ui = FakeSessionUI(answers=["a1", "a2"])
    orch, store = _make_orchestrator(tmp_path, config, ui)
    session_id = orch.start("https://github.com/owner/repo")

    with pytest.raises(SessionAlreadyCompleteError):
        orch.resume(session_id)


def test_resume_raises_for_session_interrupted_before_in_progress(tmp_path):
    config = _config(tmp_path)
    ui = FakeSessionUI(answers=[])
    orch, store = _make_orchestrator(tmp_path, config, ui)
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.update_status("sess1", "ANALYZING")  # crashed mid-setup

    with pytest.raises(SessionNotResumableError):
        orch.resume("sess1")


def test_resume_raises_for_failed_session(tmp_path):
    config = _config(tmp_path)
    ui = FakeSessionUI(answers=[])
    orch, store = _make_orchestrator(tmp_path, config, ui)
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.set_failed("sess1", "clone failed")

    with pytest.raises(SessionNotResumableError):
        orch.resume("sess1")


def test_resume_continues_pending_items(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _patch_pipeline(monkeypatch)
    ui = FakeSessionUI(answers=["a1"])
    orch, store = _make_orchestrator(tmp_path, config, ui)

    # Simulate a crash right after setup: session reached IN_PROGRESS with
    # a saved profile and plan, but no questions asked yet.
    from viva.profile import ProjectProfile

    profile = ProjectProfile.build(_fake_ingest_result(), _fake_analysis_result())
    profile_path = tmp_path / "sess1-profile.json"
    profile.save(profile_path)

    store.create_session("sess1", "https://github.com/owner/repo", "main", None, 1800)
    store.set_pipeline_artifacts(
        "sess1", repo_slug="owner/repo", commit_sha="abc123",
        collection_name="owner--repo-abc123", profile_path=str(profile_path),
    )
    store.save_plan("sess1", [QuestionPlanItem(id="q1", category="architecture", target_module=None)])
    store.update_status("sess1", "IN_PROGRESS")

    orch.resume("sess1")

    record = store.get_session("sess1")
    assert record.status == "COMPLETE"
    assert ui.summary.questions_answered == 1


def test_select_next_item_collapses_when_time_short(tmp_path):
    config = _config(tmp_path, avg_time_per_category_seconds=1_000_000)
    ui = FakeSessionUI(answers=[])
    orch, store = _make_orchestrator(tmp_path, config, ui)
    store.create_session("sess1", "https://github.com/o/r", None, None, 60)
    plan = [
        QuestionPlanItem(id="q1", category="architecture", target_module=None),
        QuestionPlanItem(id="q2", category="architecture", target_module=None, target_file="b.py"),
        QuestionPlanItem(id="q3", category="implementation_detail", target_module="core"),
    ]
    store.save_plan("sess1", plan)

    from viva.timer import AnswerTimer

    timer = AnswerTimer(60)
    timer.start()
    pending = store.get_pending_plan_items("sess1")

    selected = orch._select_next_item("sess1", pending, timer)

    assert selected is not None
    assert selected.question_id in ("q1", "q3")  # first-per-category survivor
    records = {r.question_id: r for r in store.get_qa_records("sess1")}
    # q2 shares "architecture" with q1 and arrives second -- collapsed away.
    assert records["q2"].status == SKIPPED_TIME_COLLAPSE


def test_select_next_item_does_not_collapse_with_ample_time(tmp_path):
    config = _config(tmp_path, avg_time_per_category_seconds=1)
    ui = FakeSessionUI(answers=[])
    orch, store = _make_orchestrator(tmp_path, config, ui)
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    plan = _fake_plan()
    store.save_plan("sess1", plan)

    from viva.timer import AnswerTimer

    timer = AnswerTimer(1800)
    timer.start()
    pending = store.get_pending_plan_items("sess1")

    selected = orch._select_next_item("sess1", pending, timer)

    assert selected.question_id == "q1"
    records = {r.question_id: r for r in store.get_qa_records("sess1")}
    assert records["q2"].status == "pending"  # nothing collapsed


class _AlwaysPartialProvider(ClassificationProvider):
    def classify(self, question_id, answer_text):
        return "partial"


def test_maybe_queue_followup_adds_item_when_classification_available(tmp_path):
    config = _config(tmp_path, max_followup_depth=1)
    ui = FakeSessionUI(answers=[])
    store = SessionStore(str(tmp_path / "viva.db"))
    orch = Orchestrator(
        config=config, session_store=store, ui=ui,
        llm_client=object(), embedding_client=object(), vector_store=object(),
        classification_provider=_AlwaysPartialProvider(),
    )
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    item = QuestionPlanItem(id="q1", category="architecture", target_module=None)
    store.save_plan("sess1", [item])
    store.record_question_asked("sess1", "q1", "text", [])
    qa_row = {r.question_id: r for r in store.get_qa_records("sess1")}["q1"]

    orch._maybe_queue_followup("sess1", qa_row, "a weak answer")

    pending_ids = {r.question_id for r in store.get_pending_plan_items("sess1")}
    assert "q1_f1" in pending_ids


def test_maybe_queue_followup_respects_max_depth(tmp_path):
    config = _config(tmp_path, max_followup_depth=1)
    ui = FakeSessionUI(answers=[])
    store = SessionStore(str(tmp_path / "viva.db"))
    orch = Orchestrator(
        config=config, session_store=store, ui=ui,
        llm_client=object(), embedding_client=object(), vector_store=object(),
        classification_provider=_AlwaysPartialProvider(),
    )
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    item = QuestionPlanItem(id="q1", category="architecture", target_module=None)
    store.save_plan("sess1", [item])
    store.record_question_asked("sess1", "q1", "text", [])
    qa_row = {r.question_id: r for r in store.get_qa_records("sess1")}["q1"]

    orch._maybe_queue_followup("sess1", qa_row, "weak")  # depth 0 -> 1, allowed
    followup_row = {r.question_id: r for r in store.get_qa_records("sess1")}["q1_f1"]
    orch._maybe_queue_followup("sess1", followup_row, "still weak")  # depth 1 already at max

    ids = {r.question_id for r in store.get_qa_records("sess1")}
    assert ids == {"q1", "q1_f1"}  # no q1_f2 -- max_followup_depth honored


def test_null_classification_provider_never_queues_followup(tmp_path):
    """Phase 6's real default -- the FR14 branch is unreachable end-to-end."""
    config = _config(tmp_path)
    ui = FakeSessionUI(answers=["weak answer", "weak answer"])
    store = SessionStore(str(tmp_path / "viva.db"))
    orch = Orchestrator(
        config=config, session_store=store, ui=ui,
        llm_client=object(), embedding_client=object(), vector_store=object(),
    )
    assert orch.classification_provider.classify("q1", "anything") is None
