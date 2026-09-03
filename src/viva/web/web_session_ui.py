"""`WebSessionUI`: the second real `SessionUI` implementation (docs/plan.md
Phase 10, docs/system-design/15-phase-10-web-ui-design.md §15.3).

`RichSessionUI` (`session_ui.py`) blocks the calling thread on a real
terminal; an HTTP request/response cycle can't block the same way for
however long a person takes to type an answer. `WebSessionUI` bridges
the gap with a `queue.Queue`: `read_answer()` blocks the *Orchestrator's*
background thread (spawned by `registry.py`) on that queue -- never an
HTTP request thread. `snapshot()` and `submit_answer()` are the only
methods an HTTP request handler ever calls; every other method is called
exclusively from the Orchestrator's own thread. `self._lock` is what
makes sharing state between those two threads safe -- the same "one
lock, fully serialized, cheap next to an LLM call" posture
`SessionStore` (`storage/session_store.py`) already applies to its own
cross-thread access.
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, replace

from viva.session_ui import SessionSummary, SessionUI
from viva.timer import AnswerTimer

# Stages a WebSessionState can be in -- collapsed from what a person
# watching RichSessionUI's terminal output would see down to what a
# polling client actually needs to render: either "something's
# happening, no input needed" (WORKING) or "your turn" (AWAITING_ANSWER),
# plus the three ways a session stops asking questions.
STAGE_STARTING = "starting"
STAGE_WORKING = "working"
STAGE_AWAITING_ANSWER = "awaiting_answer"
STAGE_TIME_EXPIRED = "time_expired"
STAGE_COMPLETE = "complete"
STAGE_ERROR = "error"


@dataclass(frozen=True)
class WebSessionState:
    """Immutable snapshot `WebSessionUI` hands to a polling request.
    `session_id` starts `None` and is filled in by `session_started()` --
    for a fresh `viva start`-equivalent, the id doesn't exist until the
    Orchestrator generates it (`registry.py`'s `start_session` waits on
    exactly this field)."""

    session_id: str | None = None
    stage: str = STAGE_STARTING
    detail: str | None = None
    question_text: str | None = None
    category: str | None = None
    question_number: int | None = None
    error_message: str | None = None
    summary: dict | None = None


class WebSessionUI(SessionUI):
    def __init__(self) -> None:
        self._answer_queue: "queue.Queue[str]" = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._state = WebSessionState()
        self._id_ready = threading.Event()
        self._timer: AnswerTimer | None = None
        # Set by registry.py's shutdown() so a thread still blocked in
        # read_answer() (server process exiting mid-session) doesn't hang
        # shutdown indefinitely -- read_answer polls this every 0.5s
        # instead of blocking forever.
        self._shutdown = threading.Event()

    # -- SessionUI -- called only from the Orchestrator's own thread --------

    def session_started(self, session_id: str) -> None:
        self._update(session_id=session_id, stage=STAGE_STARTING)
        self._id_ready.set()

    def stage_started(self, stage: str) -> None:
        self._update(stage=STAGE_WORKING, detail=f"{stage}...")

    def stage_completed(self, stage: str, detail: str) -> None:
        self._update(stage=STAGE_WORKING, detail=f"{stage} complete -- {detail}")

    def ask_question(self, question_text: str, category: str, question_number: int) -> None:
        self._update(
            stage=STAGE_AWAITING_ANSWER,
            detail=None,
            question_text=question_text,
            category=category,
            question_number=question_number,
        )

    def read_answer(self, timer: AnswerTimer) -> str:
        self._timer = timer
        while True:
            try:
                answer = self._answer_queue.get(timeout=0.5)
                break
            except queue.Empty:
                if self._shutdown.is_set():
                    answer = ""
                    break
        # The Orchestrator never calls the UI again between an answer
        # being recorded and either the next ask_question() or
        # session_complete() -- classification/follow-up planning happen
        # in that gap with no UI hook (design doc §15.3). Without this,
        # a poller would keep seeing the just-answered question as still
        # "awaiting_answer" and could let a person submit twice.
        self._update(
            stage=STAGE_WORKING, detail="Answer recorded -- processing...",
            question_text=None, category=None,
        )
        return answer

    def time_expired(self) -> None:
        # Not actually called by Orchestrator._run_live_session today --
        # RichSessionUI's own read_answer() checks timer.expired()
        # independently instead (see session_ui.py). Implemented anyway
        # for SessionUI contract completeness, in case that changes.
        self._update(stage=STAGE_TIME_EXPIRED, detail="Time is up.")

    def session_complete(self, summary: SessionSummary) -> None:
        self._update(
            stage=STAGE_COMPLETE,
            detail=None,
            question_text=None,
            category=None,
            summary={
                "session_id": summary.session_id,
                "status": summary.status,
                "questions_asked": summary.questions_asked,
                "questions_answered": summary.questions_answered,
                "questions_skipped": summary.questions_skipped,
            },
        )

    def error(self, message: str) -> None:
        self._update(stage=STAGE_ERROR, error_message=message)

    # -- called only from FastAPI request-handler threads --------------------

    def submit_answer(self, text: str) -> bool:
        """Returns False (the /answer route turns this into a 409) if the
        session isn't currently waiting on an answer, or already has one
        queued (a double-submit race)."""
        with self._lock:
            if self._state.stage != STAGE_AWAITING_ANSWER:
                return False
        try:
            self._answer_queue.put_nowait(text)
        except queue.Full:
            return False
        return True

    def snapshot(self) -> dict:
        with self._lock:
            state = self._state
        return {
            "session_id": state.session_id,
            "stage": state.stage,
            "detail": state.detail,
            "question_text": state.question_text,
            "category": state.category,
            "question_number": state.question_number,
            "error_message": state.error_message,
            "summary": state.summary,
            # Computed fresh on every snapshot (not baked into
            # WebSessionState) -- it's a live clock read, and a frozen
            # dataclass field would go stale between polls even though
            # nothing else about the state changed.
            "remaining_seconds": self._timer.remaining() if self._timer is not None else None,
        }

    def request_shutdown(self) -> None:
        self._shutdown.set()

    @property
    def session_id(self) -> str | None:
        with self._lock:
            return self._state.session_id

    @property
    def id_ready(self) -> bool:
        return self._id_ready.is_set()

    def _update(self, **changes: object) -> None:
        with self._lock:
            self._state = replace(self._state, **changes)
