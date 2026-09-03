"""Tests for `WebSessionUI` (docs/plan.md Phase 10, docs/system-design/
15-phase-10-web-ui-design.md §15.3/§15.10).

Exercised directly, single-threaded: `submit_answer()` pushes into the
same queue `read_answer()` reads from, so a test can call
`submit_answer()` then `read_answer()` in sequence without spinning a
real thread -- the queue itself is what's under test, not thread timing.
The real background-thread wiring is `registry.py`'s job, covered in
`test_web_registry.py`.
"""
from __future__ import annotations

from viva.session_ui import SessionSummary
from viva.timer import AnswerTimer
from viva.web.web_session_ui import (
    STAGE_AWAITING_ANSWER,
    STAGE_COMPLETE,
    STAGE_ERROR,
    STAGE_STARTING,
    STAGE_WORKING,
    WebSessionUI,
)


def _started_timer() -> AnswerTimer:
    timer = AnswerTimer(60.0)
    timer.start()
    return timer


def test_initial_state_is_starting_with_no_session_id():
    ui = WebSessionUI()

    snapshot = ui.snapshot()

    assert snapshot["stage"] == STAGE_STARTING
    assert snapshot["session_id"] is None
    assert ui.id_ready is False


def test_session_started_sets_session_id_and_id_ready():
    ui = WebSessionUI()

    ui.session_started("sess123")

    assert ui.session_id == "sess123"
    assert ui.id_ready is True
    assert ui.snapshot()["stage"] == STAGE_STARTING


def test_stage_started_and_completed_report_progress():
    ui = WebSessionUI()

    ui.stage_started("Cloning and sampling repo")
    assert ui.snapshot()["stage"] == STAGE_WORKING
    assert "Cloning" in ui.snapshot()["detail"]

    ui.stage_completed("Ingest", "12/12 files sampled")
    detail = ui.snapshot()["detail"]
    assert "Ingest" in detail
    assert "12/12 files sampled" in detail


def test_ask_question_sets_awaiting_answer_with_question_fields():
    ui = WebSessionUI()

    ui.ask_question("What does this function do?", "architecture", 1)

    snapshot = ui.snapshot()
    assert snapshot["stage"] == STAGE_AWAITING_ANSWER
    assert snapshot["question_text"] == "What does this function do?"
    assert snapshot["category"] == "architecture"
    assert snapshot["question_number"] == 1


def test_submit_answer_rejected_when_not_awaiting_answer():
    ui = WebSessionUI()  # still STAGE_STARTING -- nothing asked yet

    accepted = ui.submit_answer("an answer")

    assert accepted is False


def test_submit_answer_accepted_and_read_answer_returns_it():
    ui = WebSessionUI()
    ui.ask_question("Why is this a dataclass?", "design", 3)

    accepted = ui.submit_answer("Because it's an immutable value object.")
    assert accepted is True

    answer = ui.read_answer(_started_timer())

    assert answer == "Because it's an immutable value object."


def test_read_answer_clears_question_and_moves_to_working():
    ui = WebSessionUI()
    ui.ask_question("Why is this a dataclass?", "design", 3)
    ui.submit_answer("Because it's an immutable value object.")

    ui.read_answer(_started_timer())

    snapshot = ui.snapshot()
    assert snapshot["stage"] == STAGE_WORKING
    assert snapshot["question_text"] is None
    assert snapshot["category"] is None


def test_double_submit_second_call_rejected():
    ui = WebSessionUI()
    ui.ask_question("Q", "design", 1)

    first = ui.submit_answer("first answer")
    second = ui.submit_answer("second answer")

    assert first is True
    assert second is False  # queue (maxsize=1) already full


def test_snapshot_includes_remaining_seconds_once_a_timer_is_known():
    ui = WebSessionUI()
    assert ui.snapshot()["remaining_seconds"] is None  # no read_answer() call yet

    ui.ask_question("Q", "design", 1)
    ui.submit_answer("answer")
    ui.read_answer(_started_timer())

    remaining = ui.snapshot()["remaining_seconds"]
    assert remaining is not None
    assert 0 < remaining <= 60.0


def test_session_complete_sets_summary_and_clears_question():
    ui = WebSessionUI()
    ui.ask_question("Q", "design", 1)

    ui.session_complete(
        SessionSummary(
            session_id="sess123", status="COMPLETE",
            questions_asked=5, questions_answered=5, questions_skipped=0,
        )
    )

    snapshot = ui.snapshot()
    assert snapshot["stage"] == STAGE_COMPLETE
    assert snapshot["question_text"] is None
    assert snapshot["summary"] == {
        "session_id": "sess123", "status": "COMPLETE",
        "questions_asked": 5, "questions_answered": 5, "questions_skipped": 0,
    }


def test_error_sets_stage_and_message():
    ui = WebSessionUI()

    ui.error("Clone failed: repository not found")

    snapshot = ui.snapshot()
    assert snapshot["stage"] == STAGE_ERROR
    assert snapshot["error_message"] == "Clone failed: repository not found"


def test_request_shutdown_unblocks_read_answer_with_empty_string():
    ui = WebSessionUI()
    ui.ask_question("Q", "design", 1)  # awaiting_answer, but nobody submits

    ui.request_shutdown()
    answer = ui.read_answer(_started_timer())

    assert answer == ""
