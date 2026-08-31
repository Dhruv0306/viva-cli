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
from viva.storage.session_store import ANSWERED


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
        self.events.append(("stage_completed", stage, detail))

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
        avg_time_per_category_seconds=1, question_similarity_threshold=0.90,
        eval_flush_timeout_seconds=1,
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


def _patch_pipeline(monkeypatch, plan=None, grounded=True, reused_collection=False):
    monkeypatch.setattr(orchestrator_module, "ingest_repo", lambda *a, **kw: _fake_ingest_result())
    monkeypatch.setattr(orchestrator_module, "analyze_repo", lambda *a, **kw: _fake_analysis_result())
    monkeypatch.setattr(
        orchestrator_module, "index_repo",
        lambda *a, **kw: IndexResult(
            collection_name="owner--repo-abc123",
            stats=IndexStats(
                files_processed=0 if reused_collection else 2,
                chunks_built=0 if reused_collection else 4,
                reused_existing_collection=reused_collection,
            ),
        ),
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


class FakeEmbeddingClient:
    """Deterministic 1-D 'embedding': identical text -> identical vector,
    different text -> (almost certainly) different vector. Good enough
    for tests that don't specifically exercise duplicate-similarity
    logic; tests that do (see test_is_semantic_duplicate_*) construct
    exact vectors directly instead."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(hash(text) % 100_000)] for text in texts]


def _make_orchestrator(tmp_path, config, ui):
    store = SessionStore(str(tmp_path / "viva.db"))
    orch = Orchestrator(
        config=config, session_store=store, ui=ui,
        llm_client=object(), embedding_client=FakeEmbeddingClient(), vector_store=object(),
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


def test_reused_collection_reports_reuse_not_zero_chunks(tmp_path, monkeypatch):
    """Regression test for a real-world bug: a second session against the
    same commit correctly reuses the existing Chroma collection
    (index_repo() returns chunks_built=0, reused_existing_collection=True
    -- see indexer/__init__.py), but the original message
    ("0 chunk(s) indexed") read as if indexing had silently failed, even
    though retrieval worked fine off the reused collection. Found running
    `viva start` twice against github.com/Dhruv0306/throttle4j at the same
    commit."""
    config = _config(tmp_path)
    _patch_pipeline(monkeypatch, reused_collection=True)
    ui = FakeSessionUI(answers=["answer one", "answer two"])
    orch, store = _make_orchestrator(tmp_path, config, ui)

    orch.start("https://github.com/owner/repo")

    indexing_events = [e for e in ui.events if e[0] == "stage_completed" and e[1] == "Indexing"]
    assert len(indexing_events) == 1
    detail = indexing_events[0][2]
    assert "0 chunk" not in detail
    assert "reus" in detail.lower()


def test_freshly_built_collection_reports_chunk_count(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _patch_pipeline(monkeypatch, reused_collection=False)
    ui = FakeSessionUI(answers=["answer one", "answer two"])
    orch, store = _make_orchestrator(tmp_path, config, ui)

    orch.start("https://github.com/owner/repo")

    indexing_events = [e for e in ui.events if e[0] == "stage_completed" and e[1] == "Indexing"]
    assert indexing_events[0][2] == "4 chunk(s) indexed"


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


def test_resume_reasks_orphaned_unanswered_question(tmp_path, monkeypatch):
    """Regression test for a real-world bug found running `viva resume`
    against an interrupted session on github.com/Dhruv0306/throttle4j:
    the process crashed while a question was displayed, before the
    answer was captured. The old behavior left that question stuck at
    'asked' forever -- resume silently never gave the person a chance to
    answer it, and the summary undercounted (asked=N, answered=N-1) with
    no accounting for the gap.
    """
    config = _config(tmp_path)
    _patch_pipeline(monkeypatch)
    ui = FakeSessionUI(answers=["answer to the orphaned question"])
    orch, store = _make_orchestrator(tmp_path, config, ui)

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
    # Simulate the crash: q1 was shown but never answered.
    store.record_question_asked("sess1", "q1", "What does this do?", ["chunk1"])

    orch.resume("sess1")

    record = {r.question_id: r for r in store.get_qa_records("sess1")}["q1"]
    assert record.status == ANSWERED
    assert record.answer_text == "answer to the orphaned question"
    assert ("ask", 1, "architecture") in ui.events  # re-presented, not skipped
    assert ui.summary.questions_asked == 1
    assert ui.summary.questions_answered == 1  # no more asked/answered mismatch
    assert ui.summary.questions_skipped == 0


def test_resume_does_not_regenerate_orphaned_question(tmp_path, monkeypatch):
    """The requeued question's text/grounding were already generated
    before the crash -- resuming shouldn't pay for another LLM call."""
    config = _config(tmp_path)
    _patch_pipeline(monkeypatch)
    generate_calls = []
    original = orchestrator_module.generate_question

    def counting_generate_question(*a, **kw):
        generate_calls.append(1)
        return original(*a, **kw)

    monkeypatch.setattr(orchestrator_module, "generate_question", counting_generate_question)

    ui = FakeSessionUI(answers=["an answer"])
    orch, store = _make_orchestrator(tmp_path, config, ui)

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
    store.record_question_asked("sess1", "q1", "Pre-generated question text?", ["chunk1"])

    orch.resume("sess1")

    assert generate_calls == []  # never called -- reused the persisted text
    record = {r.question_id: r for r in store.get_qa_records("sess1")}["q1"]
    assert record.question_text == "Pre-generated question text?"


def test_select_next_item_prefers_non_duplicate_target(tmp_path):
    """Regression test for a real-world bug found running `viva start`
    against github.com/Dhruv0306/throttle4j: two plan items in different
    categories both targeted FixedWindowLimiter, and nothing stopped the
    second from being asked -- producing a near-identical question twice
    in the same session. FR15 (docs/requirements.md): "Track asked
    topics/files to avoid duplicate questioning...".

    This is a preference, not a hard exclusion (see
    _select_next_item's docstring for why an earlier version that
    permanently dropped duplicates caused a worse regression): q3
    (a novel target) should be preferred over q2 (a duplicate of q1's
    target), but q2 must stay 'pending', available if genuinely needed
    later -- not permanently skipped.
    """
    config = _config(tmp_path, avg_time_per_category_seconds=1)
    ui = FakeSessionUI(answers=[])
    orch, store = _make_orchestrator(tmp_path, config, ui)
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    plan = [
        QuestionPlanItem(id="q1", category="architecture", target_module=None,
                          target_file="FixedWindowLimiter.java"),
        QuestionPlanItem(id="q2", category="implementation_detail", target_module=None,
                          target_file="FixedWindowLimiter.java"),
        QuestionPlanItem(id="q3", category="testing", target_module=None, target_file="Other.java"),
    ]
    store.save_plan("sess1", plan)
    # q1 already asked this session -- same target_file as q2.
    store.record_question_asked("sess1", "q1", "Q1 text", [])

    from viva.timer import AnswerTimer

    timer = AnswerTimer(1800)
    timer.start()
    pending = store.get_pending_plan_items("sess1")

    selected = orch._select_next_item("sess1", pending, timer)

    assert selected.question_id == "q3"  # non-duplicate preferred
    record = {r.question_id: r for r in store.get_qa_records("sess1")}["q2"]
    assert record.status == "pending"  # deprioritized, not dropped


def test_select_next_item_does_not_flag_distinct_targets(tmp_path):
    config = _config(tmp_path, avg_time_per_category_seconds=1)
    ui = FakeSessionUI(answers=[])
    orch, store = _make_orchestrator(tmp_path, config, ui)
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    plan = [
        QuestionPlanItem(id="q1", category="architecture", target_module=None, target_file="A.java"),
        QuestionPlanItem(id="q2", category="testing", target_module=None, target_file="B.java"),
    ]
    store.save_plan("sess1", plan)
    store.record_question_asked("sess1", "q1", "Q1 text", [])

    from viva.timer import AnswerTimer

    timer = AnswerTimer(1800)
    timer.start()
    pending = store.get_pending_plan_items("sess1")

    selected = orch._select_next_item("sess1", pending, timer)

    assert selected.question_id == "q2"
    record = {r.question_id: r for r in store.get_qa_records("sess1")}["q2"]
    assert record.status == "pending"  # not flagged -- distinct target


def test_select_next_item_prefers_novel_category_but_keeps_repeat_pending(tmp_path):
    """Category-breadth preference (design.md §7) is ordering only, never
    exclusion -- see _select_next_item's docstring for the real-world
    regression (a permanent, one-time collapse decision capped sessions
    at a fixed question count regardless of pacing) that this replaced.
    """
    config = _config(tmp_path)
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

    assert selected.question_id == "q1"  # first item, nothing asked yet -- no preference difference
    records = {r.question_id: r for r in store.get_qa_records("sess1")}
    assert records["q2"].status == "pending"  # never permanently dropped

    # Once q1 (architecture) is asked, q3 (a novel category) should be
    # preferred over q2 (a repeat of q1's category) -- but q2 stays
    # available, not skipped.
    store.record_question_asked("sess1", "q1", "Q1 text", [])
    store.record_answer("sess1", "q1", "answer")
    pending = store.get_pending_plan_items("sess1")
    selected = orch._select_next_item("sess1", pending, timer)
    assert selected.question_id == "q3"
    assert records["q2"].status == "pending"


def test_select_next_item_eventually_asks_repeat_category_when_nothing_novel_left(tmp_path):
    """The real regression: once every distinct category has been
    covered, remaining same-category items must still be selectable --
    the session should keep going until it truly runs out of pending
    items or time, not stop artificially at (category count)."""
    config = _config(tmp_path)
    ui = FakeSessionUI(answers=[])
    orch, store = _make_orchestrator(tmp_path, config, ui)
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    plan = [
        QuestionPlanItem(id="q1", category="architecture", target_module=None, target_file="a.py"),
        QuestionPlanItem(id="q2", category="architecture", target_module=None, target_file="b.py"),
    ]
    store.save_plan("sess1", plan)
    store.record_question_asked("sess1", "q1", "Q1 text", [])
    store.record_answer("sess1", "q1", "answer")

    from viva.timer import AnswerTimer

    timer = AnswerTimer(1800)
    timer.start()
    pending = store.get_pending_plan_items("sess1")

    selected = orch._select_next_item("sess1", pending, timer)

    assert selected is not None
    assert selected.question_id == "q2"  # only option left -- must still be returned, not None



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


def test_small_repo_does_not_prematurely_exhaust_plan(tmp_path):
    """Regression test for a real-world bug found running 'viva start'
    against github.com/Dhruv0306/throttle4j with --duration 8: a small
    repo has far fewer distinct files than planned questions, so multiple
    categories necessarily target the same file. The FR15 fix (previous
    patch) treated 'same target file, any category' as a permanent skip,
    which on a small repo caused most of an 8-question plan to be dropped
    after only 2 questions -- asked=2, skipped=6, session COMPLETE with
    75% of the plan never even attempted, even though ~7 minutes of the
    8-minute budget remained.
    """
    config = _config(tmp_path, avg_time_per_category_seconds=180)
    ui = FakeSessionUI(answers=["a"] * 8)
    orch, store = _make_orchestrator(tmp_path, config, ui)
    store.create_session("sess1", "https://github.com/o/r", None, None, 480)
    # Mirrors throttle4j: only 3 distinct files, 5 categories, 8 items --
    # by pigeonhole, several categories must share a target file.
    plan = [
        QuestionPlanItem(id="q1", category="architecture", target_module=None, target_file="A.java"),
        QuestionPlanItem(id="q2", category="implementation_detail", target_module=None, target_file="A.java"),
        QuestionPlanItem(id="q3", category="testing", target_module=None, target_file="B.java"),
        QuestionPlanItem(id="q4", category="edge_case", target_module=None, target_file="B.java"),
        QuestionPlanItem(id="q5", category="historical_rationale", target_module=None, target_file="C.java"),
        QuestionPlanItem(id="q6", category="architecture", target_module=None, target_file="C.java"),
        QuestionPlanItem(id="q7", category="implementation_detail", target_module=None, target_file="B.java"),
        QuestionPlanItem(id="q8", category="testing", target_module=None, target_file="A.java"),
    ]
    store.save_plan("sess1", plan)

    from viva.timer import AnswerTimer

    timer = AnswerTimer(480)
    timer.start()

    # Simulate asking q1 (A.java) and q3 (B.java), same as the real run.
    store.record_question_asked("sess1", "q1", "Q1 text", [])
    store.record_answer("sess1", "q1", "a")
    store.record_question_asked("sess1", "q3", "Q3 text", [])
    store.record_answer("sess1", "q3", "a")

    pending = store.get_pending_plan_items("sess1")
    selected = orch._select_next_item("sess1", pending, timer)

    # Every remaining item duplicates an already-asked file (A or B) except
    # q5/q6 (C.java) -- the fix should still select one of those, not
    # give up because a *majority* of items happen to duplicate.
    assert selected is not None
    assert selected.question_id in ("q5", "q6")


def test_falls_back_to_duplicate_target_rather_than_ending_session(tmp_path):
    """The sharper version of the above: a repo small enough that
    *every* remaining plan item duplicates an already-asked file. The
    old (buggy) behavior marked all of them skipped_duplicate_target and
    gave up, ending the session with time and pending questions still
    left. The fix must still pick one rather than returning None."""
    config = _config(tmp_path, avg_time_per_category_seconds=180)
    ui = FakeSessionUI(answers=[])
    orch, store = _make_orchestrator(tmp_path, config, ui)
    store.create_session("sess1", "https://github.com/o/r", None, None, 480)
    plan = [
        QuestionPlanItem(id="q1", category="architecture", target_module=None, target_file="A.java"),
        QuestionPlanItem(id="q2", category="implementation_detail", target_module=None, target_file="A.java"),
        QuestionPlanItem(id="q3", category="testing", target_module=None, target_file="B.java"),
    ]
    store.save_plan("sess1", plan)
    store.record_question_asked("sess1", "q1", "Q1 text", [])
    store.record_answer("sess1", "q1", "a")
    store.record_question_asked("sess1", "q3", "Q3 text", [])
    store.record_answer("sess1", "q3", "a")
    # Only q2 remains, and it duplicates q1's target (A.java) -- there is
    # no non-duplicate alternative left at all.

    from viva.timer import AnswerTimer

    timer = AnswerTimer(480)
    timer.start()
    pending = store.get_pending_plan_items("sess1")

    selected = orch._select_next_item("sess1", pending, timer)

    assert selected is not None
    assert selected.question_id == "q2"
    # It was not permanently dropped -- still asked, just deprioritized.
    record = {r.question_id: r for r in store.get_qa_records("sess1")}["q2"]
    assert record.status == "pending"


def test_collapse_does_not_permanently_cap_session_regardless_of_pace(tmp_path):
    """Regression test for a real-world bug: two real sessions against
    github.com/Dhruv0306/throttle4j, run with --duration 8 and
    --duration 10 respectively, both stopped at exactly 5 of 8 planned
    questions -- identical outcome despite the 2-minute difference in
    budget. Root cause: with the default AVG_TIME_PER_CATEGORY_SECONDS
    (180s) and 5 planned categories, budget_needed = 5*180 = 900s (15
    minutes) is already greater than either --duration, so the collapse
    check fires on the very FIRST selection (before anything is asked)
    and permanently marks every 'extra' item skipped -- locking in
    exactly 5 survivors regardless of how fast the person actually
    answers or how much real time is left afterward.
    """
    config = _config(tmp_path, avg_time_per_category_seconds=180)
    ui = FakeSessionUI(answers=[])
    orch, store = _make_orchestrator(tmp_path, config, ui)
    store.create_session("sess1", "https://github.com/o/r", None, None, 480)
    # 5 categories, 8 items -- mirrors the real plan shape.
    plan = [
        QuestionPlanItem(id="q1", category="architecture", target_module=None, target_file="A.java"),
        QuestionPlanItem(id="q2", category="implementation_detail", target_module=None, target_file="B.java"),
        QuestionPlanItem(id="q3", category="testing", target_module=None, target_file="C.java"),
        QuestionPlanItem(id="q4", category="edge_case", target_module=None, target_file="D.java"),
        QuestionPlanItem(id="q5", category="historical_rationale", target_module=None, target_file="E.java"),
        QuestionPlanItem(id="q6", category="architecture", target_module=None, target_file="F.java"),
        QuestionPlanItem(id="q7", category="implementation_detail", target_module=None, target_file="G.java"),
        QuestionPlanItem(id="q8", category="testing", target_module=None, target_file="H.java"),
    ]
    store.save_plan("sess1", plan)

    from viva.timer import AnswerTimer

    timer = AnswerTimer(480)
    timer.start()  # nothing asked yet, t=0 -- exactly the real failure state
    pending = store.get_pending_plan_items("sess1")

    orch._select_next_item("sess1", pending, timer)

    # The old behavior marked q6/q7/q8 permanently skipped right here,
    # before even asking q1 -- with 480s (8 minutes) still on the clock
    # and only ~a few seconds actually elapsed.
    still_available = {r.question_id for r in store.get_qa_records("sess1") if r.status == "pending"}
    assert {"q6", "q7", "q8"} <= still_available


# -- FR15's third layer: embedding-based semantic duplicate detection -------


def test_cosine_similarity_identical_vectors():
    assert orchestrator_module._cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors():
    assert orchestrator_module._cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_zero_vector_returns_zero():
    assert orchestrator_module._cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0


def test_is_semantic_duplicate_true_above_threshold(tmp_path):
    config = _config(tmp_path, question_similarity_threshold=0.9)
    ui = FakeSessionUI(answers=[])
    orch, _store = _make_orchestrator(tmp_path, config, ui)
    orch._question_embeddings["q1"] = [1.0, 0.0]

    assert orch._is_semantic_duplicate([0.99, 0.01]) is True  # nearly identical direction


def test_is_semantic_duplicate_false_below_threshold(tmp_path):
    config = _config(tmp_path, question_similarity_threshold=0.9)
    ui = FakeSessionUI(answers=[])
    orch, _store = _make_orchestrator(tmp_path, config, ui)
    orch._question_embeddings["q1"] = [1.0, 0.0]

    assert orch._is_semantic_duplicate([0.0, 1.0]) is False  # orthogonal


def test_is_semantic_duplicate_false_with_empty_cache(tmp_path):
    config = _config(tmp_path)
    ui = FakeSessionUI(answers=[])
    orch, _store = _make_orchestrator(tmp_path, config, ui)

    assert orch._is_semantic_duplicate([1.0, 0.0]) is False


class _ScriptedEmbeddingClient:
    """Maps specific known texts to specific vectors (for controlling
    similarity outcomes precisely); anything unlisted gets a distinct
    hash-based vector, mirroring FakeEmbeddingClient's default."""

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self._mapping = mapping

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._mapping.get(t, [float(hash(t) % 100_000)]) for t in texts]


def test_semantic_duplicate_prefers_novel_question_when_available(tmp_path, monkeypatch):
    """Regression test for a real-world finding: two real sessions against
    github.com/Dhruv0306/throttle4j each asked essentially the same
    question twice, worded differently, for plan items whose
    target_file/target_module strings didn't match exactly -- the
    string-based dedup couldn't catch it. This is the embedding-based
    layer that can.
    """
    config = _config(tmp_path, question_similarity_threshold=0.9)
    _patch_pipeline(monkeypatch)

    # q1's generated text and q2's are near-identical in embedding space
    # (same direction); q3's is orthogonal (genuinely different).
    scripted = _ScriptedEmbeddingClient({
        "Question about architecture?": [1.0, 0.0],
        "A totally different question.": [0.0, 1.0],
    })

    def fake_generate_question(plan_item, *a, **kw):
        text_by_id = {
            "q1": "Question about architecture?",
            "q2": "Question about architecture?",  # near-duplicate of q1 in embedding space
            "q3": "A totally different question.",
        }
        return GeneratedQuestion(
            plan_item=plan_item, question_text=text_by_id[plan_item.id], grounding_chunk_ids=["c1"],
        )

    monkeypatch.setattr(orchestrator_module, "generate_question", fake_generate_question)
    monkeypatch.setattr(
        orchestrator_module, "build_coverage_plan",
        lambda *a, **kw: [
            QuestionPlanItem(id="q1", category="architecture", target_module=None, target_file="A.java"),
            QuestionPlanItem(id="q2", category="testing", target_module=None, target_file="B.java"),
            QuestionPlanItem(id="q3", category="edge_case", target_module=None, target_file="C.java"),
        ],
    )

    ui = FakeSessionUI(answers=["a1", "a2", "a3"])
    store = SessionStore(str(tmp_path / "viva.db"))
    orch = Orchestrator(
        config=config, session_store=store, ui=ui,
        llm_client=object(), embedding_client=scripted, vector_store=object(),
    )

    orch.start("https://github.com/owner/repo")

    # q3 (novel) should be preferred over q2 (a near-duplicate of q1) when
    # both are available -- asked before it, not instead of it. With only
    # 3 total plan items and no time pressure, q2 still eventually gets
    # asked once nothing else is left (prefer, never exclude -- same
    # discipline as the other two fixes), but its position should reflect
    # having been deprioritized.
    ask_events = [e for e in ui.events if e[0] == "ask"]
    order_by_category = [category for (_evt, _num, category) in ask_events]
    assert order_by_category == ["architecture", "edge_case", "testing"]


def test_semantic_duplicate_falls_back_when_nothing_novel_available(tmp_path, monkeypatch):
    """The other half of the same discipline as the previous two fixes:
    if every candidate tried is a near-duplicate, ask one anyway rather
    than silently skipping the round or ending the session early."""
    config = _config(tmp_path, question_similarity_threshold=0.9)
    _patch_pipeline(monkeypatch)

    def fake_generate_question(plan_item, *a, **kw):
        # Every candidate embeds identically -- there is no novel option.
        return GeneratedQuestion(
            plan_item=plan_item, question_text=f"Question about {plan_item.category}?",
            grounding_chunk_ids=["c1"],
        )

    monkeypatch.setattr(orchestrator_module, "generate_question", fake_generate_question)
    monkeypatch.setattr(
        orchestrator_module, "build_coverage_plan",
        lambda *a, **kw: [
            QuestionPlanItem(id="q1", category="architecture", target_module=None, target_file="A.java"),
            QuestionPlanItem(id="q2", category="testing", target_module=None, target_file="B.java"),
        ],
    )

    class _AllSameVectorEmbeddingClient:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]  # every text embeds identically

    ui = FakeSessionUI(answers=["a1", "a2"])
    store = SessionStore(str(tmp_path / "viva.db"))
    orch = Orchestrator(
        config=config, session_store=store, ui=ui,
        llm_client=object(), embedding_client=_AllSameVectorEmbeddingClient(), vector_store=object(),
    )

    orch.start("https://github.com/owner/repo")

    # Both still get asked -- the duplicate check deprioritizes, never
    # excludes, so with only 2 candidates and a 3-candidate retry bound,
    # both are eventually tried and both get answered.
    session_id = store.list_sessions()[0].session_id
    records = store.get_qa_records(session_id)
    assert sum(1 for r in records if r.status == "answered") == 2


def test_seed_embedding_cache_populates_from_prior_answers(tmp_path):
    config = _config(tmp_path)
    ui = FakeSessionUI(answers=[])
    orch, store = _make_orchestrator(tmp_path, config, ui)
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.save_plan("sess1", [QuestionPlanItem(id="q1", category="architecture", target_module=None)])
    store.record_question_asked("sess1", "q1", "Why does X work this way?", ["c1"])
    store.record_answer("sess1", "q1", "because Y")

    assert "q1" not in orch._question_embeddings
    orch._seed_embedding_cache("sess1")
    assert "q1" in orch._question_embeddings


def test_generate_question_receives_prior_question_texts(tmp_path, monkeypatch):
    """FR15's primary defense (docs/system-design/
    11-phase-6-session-loop-design.md §11.12): the LLM should see what's
    already been asked this session, not just have duplicates caught
    after the fact."""
    config = _config(tmp_path)
    _patch_pipeline(monkeypatch)
    avoid_questions_seen = []

    def recording_generate_question(plan_item, *a, avoid_questions=None, **kw):
        avoid_questions_seen.append(list(avoid_questions or []))
        return GeneratedQuestion(
            plan_item=plan_item, question_text=f"Question about {plan_item.category}?",
            grounding_chunk_ids=["c1"],
        )

    monkeypatch.setattr(orchestrator_module, "generate_question", recording_generate_question)
    ui = FakeSessionUI(answers=["a1", "a2"])
    orch, _store = _make_orchestrator(tmp_path, config, ui)

    orch.start("https://github.com/owner/repo")

    assert len(avoid_questions_seen) == 2
    assert avoid_questions_seen[0] == []  # nothing asked yet for the first question
    assert avoid_questions_seen[1] == ["Question about architecture?"]  # q1's text, for q2
