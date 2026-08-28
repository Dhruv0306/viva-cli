import pytest

from viva.questiongen.models import QuestionPlanItem
from viva.storage.session_store import (
    ANSWERED,
    ASKED,
    PENDING,
    SKIPPED_NO_GROUNDING,
    SessionStore,
)


@pytest.fixture
def store(tmp_path):
    s = SessionStore(str(tmp_path / "viva.db"))
    yield s
    s.close()


def _plan_items():
    return [
        QuestionPlanItem(id="q1", category="architecture", target_module=None),
        QuestionPlanItem(id="q2", category="implementation_detail", target_module="core"),
    ]


def test_create_session_sets_ingesting_status(store):
    store.create_session("sess1", repo_url="https://github.com/o/r", branch="main",
                          session_name="demo", duration_seconds=1800)
    record = store.get_session("sess1")
    assert record is not None
    assert record.status == "INGESTING"
    assert record.repo_url == "https://github.com/o/r"
    assert record.repo_slug is None  # not known yet, per create_session's contract
    assert record.duration_seconds == 1800


def test_get_session_missing_returns_none(store):
    assert store.get_session("nope") is None


def test_set_pipeline_artifacts_updates_fields(store):
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.set_pipeline_artifacts(
        "sess1", repo_slug="o/r", commit_sha="abc123",
        collection_name="o--r-abc123", profile_path="/tmp/profile.json",
    )
    record = store.get_session("sess1")
    assert record.repo_slug == "o/r"
    assert record.commit_sha == "abc123"
    assert record.collection_name == "o--r-abc123"
    assert record.profile_path == "/tmp/profile.json"


def test_update_status_changes_status(store):
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.update_status("sess1", "ANALYZING")
    assert store.get_session("sess1").status == "ANALYZING"


def test_set_failed_sets_status_and_message(store):
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.set_failed("sess1", "clone failed: 404")
    record = store.get_session("sess1")
    assert record.status == "FAILED"
    assert record.error_message == "clone failed: 404"


def test_list_sessions_filters_by_status(store):
    store.create_session("sess1", "https://github.com/o/r1", None, None, 1800)
    store.create_session("sess2", "https://github.com/o/r2", None, None, 1800)
    store.update_status("sess2", "COMPLETE")

    all_sessions = store.list_sessions()
    assert {s.session_id for s in all_sessions} == {"sess1", "sess2"}

    complete_only = store.list_sessions(status="COMPLETE")
    assert [s.session_id for s in complete_only] == ["sess2"]


def test_save_plan_inserts_pending_items_in_order(store):
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.save_plan("sess1", _plan_items())

    pending = store.get_pending_plan_items("sess1")
    assert [p.question_id for p in pending] == ["q1", "q2"]
    assert all(p.status == PENDING for p in pending)
    assert pending[0].category == "architecture"
    assert pending[1].target_module == "core"


def test_save_plan_is_idempotent_for_same_question_id(store):
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.save_plan("sess1", _plan_items())
    store.save_plan("sess1", _plan_items())  # INSERT OR IGNORE -- no duplicate rows

    assert len(store.get_qa_records("sess1")) == 2


def test_record_question_asked_updates_status_and_grounding(store):
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.save_plan("sess1", _plan_items())

    store.record_question_asked("sess1", "q1", "How does X work?", ["chunk1", "chunk2"])

    records = {r.question_id: r for r in store.get_qa_records("sess1")}
    assert records["q1"].status == ASKED
    assert records["q1"].question_text == "How does X work?"
    assert records["q1"].grounding_chunk_ids == ["chunk1", "chunk2"]
    assert records["q1"].asked_at is not None
    # untouched item stays pending
    assert records["q2"].status == PENDING


def test_record_answer_updates_status_and_defers_eval(store):
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.save_plan("sess1", _plan_items())
    store.record_question_asked("sess1", "q1", "How does X work?", [])

    store.record_answer("sess1", "q1", "It works by doing Y.")

    record = {r.question_id: r for r in store.get_qa_records("sess1")}["q1"]
    assert record.status == ANSWERED
    assert record.answer_text == "It works by doing Y."
    assert record.answered_at is not None
    assert record.eval_status == "deferred"  # Phase 6: always deferred, see classification.py


def test_answered_item_no_longer_pending(store):
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.save_plan("sess1", _plan_items())
    store.record_question_asked("sess1", "q1", "Q1 text", [])
    store.record_answer("sess1", "q1", "answer")

    pending = store.get_pending_plan_items("sess1")
    assert [p.question_id for p in pending] == ["q2"]


def test_mark_item_status_skips_without_asking(store):
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.save_plan("sess1", _plan_items())

    store.mark_item_status("sess1", "q2", SKIPPED_NO_GROUNDING)

    pending = store.get_pending_plan_items("sess1")
    assert [p.question_id for p in pending] == ["q1"]
    record = {r.question_id: r for r in store.get_qa_records("sess1")}["q2"]
    assert record.status == SKIPPED_NO_GROUNDING
    assert record.question_text is None  # never generated


def test_add_followup_item_appears_as_pending(store):
    store.create_session("sess1", "https://github.com/o/r", None, None, 1800)
    store.save_plan("sess1", _plan_items())

    followup = QuestionPlanItem(
        id="q1_f1", category="architecture", target_module=None, is_followup_of="q1",
    )
    store.add_followup_item("sess1", followup)

    pending_ids = {p.question_id for p in store.get_pending_plan_items("sess1")}
    assert "q1_f1" in pending_ids
    record = {r.question_id: r for r in store.get_qa_records("sess1")}["q1_f1"]
    assert record.is_followup_of == "q1"


def test_get_qa_records_empty_for_unknown_session(store):
    assert store.get_qa_records("nope") == []
