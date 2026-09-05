"""`SessionRegistry`: in-process tracking of live web sessions
(docs/plan.md Phase 10, docs/system-design/15-phase-10-web-ui-design.md
\u00a715.3/\u00a715.4).

Tracks which sessions currently have a background thread running an
`Orchestrator.start()`/`.resume()` call in *this* process -- it is
deliberately not a durable store. `SessionStore` already durably
persists everything a session needs to survive a restart (that's what
makes `viva resume` work); if this process restarts mid-session, the
registry simply starts empty, and the existing `viva resume`
(`POST /api/sessions/{id}/resume`) semantics -- re-derived from
`SessionStore`, unchanged by this phase -- are what bring it back,
exactly as they already do for the CLI today.

One `Orchestrator`/`WebSessionUI`/`SessionStore` triple per live
session, each on its own background thread, coordinated the same "one
lock, fully serialized, cheap next to an LLM call" way `SessionStore`
itself already coordinates cross-thread access.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from viva.config import Config
from viva.orchestrator import Orchestrator, OrchestratorError
from viva.storage import SessionStore
from viva.web.web_session_ui import WebSessionUI

# How long to wait, once a session's background thread is spawned, for
# either Orchestrator.start()/resume() to report a real session_id
# (WebSessionUI.session_started -- see web_session_ui.py) or fail its own
# local validation before ever reaching that point. Both outcomes only
# involve local SQLite reads/writes, never an LLM/network call or repo
# analysis -- session_started() fires before Orchestrator.start() ever
# clones/analyzes anything (design doc \u00a715.3) -- so this resolves in
# milliseconds in practice regardless of repo size or how slow the local
# model is. Bumped from an original 10s to a much more generous 120s as
# headroom against exactly that "slower local model, bigger repo"
# concern anyway: it costs nothing (this wait is never actually
# exercised for anywhere near that long), and it's the one genuine
# timeout construct that exists anywhere in the web layer -- everywhere
# else, repo analysis runs on its own background thread with no
# deadline at all, same as it always has for the CLI.
_SESSION_ID_WAIT_SECONDS = 120.0
_POLL_INTERVAL_SECONDS = 0.02


@dataclass
class LiveSession:
    ui: WebSessionUI
    thread: threading.Thread
    store: SessionStore


class SessionRegistry:
    """One instance per `viva serve` process, owned by `app.py`."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._sessions: dict[str, LiveSession] = {}

    def start_session(
        self,
        repo_url: str,
        branch: str | None,
        duration_minutes: int | None,
        session_name: str | None,
    ) -> str:
        """Spawns a background thread running `Orchestrator.start()` and
        returns the generated session_id as soon as it exists -- mirrors
        the CLI contract's "prints session_id immediately" guarantee
        (docs/system-design/06-cli-contract-and-profile-scaling.md
        \u00a76.1): a *later* failure (bad URL, model timeout) doesn't
        prevent the id from being returned here -- it's still reachable
        afterward via `GET /api/sessions/{id}/state`, exactly like a
        failed `viva start` remains inspectable via `viva list`.
        """
        store = SessionStore(self._config.session_db_path)
        ui = WebSessionUI()
        orchestrator = Orchestrator(config=self._config, session_store=store, ui=ui)
        error_holder: dict[str, BaseException] = {}
        thread_done = threading.Event()

        def _run() -> None:
            try:
                orchestrator.start(
                    repo_url, branch=branch, duration_minutes=duration_minutes,
                    session_name=session_name,
                )
            except BaseException as exc:  # noqa: BLE001 - captured for _await_session_id below; also surfaced via ui.error() as a catch-all in case the exception came from a path (e.g. the live loop) that doesn't already call ui.error() itself
                error_holder["error"] = exc
                ui.error(str(exc))
            finally:
                store.close()
                thread_done.set()

        thread = threading.Thread(target=_run, name="viva-session-start", daemon=True)
        thread.start()

        session_id = self._await_session_id(ui, thread_done, error_holder)
        with self._lock:
            self._sessions[session_id] = LiveSession(ui=ui, thread=thread, store=store)
        return session_id

    def resume_session(self, session_id: str) -> WebSessionUI:
        """Spawns a background thread running `Orchestrator.resume()`.

        Raises whatever `Orchestrator.resume()` itself raises
        (`SessionNotFoundError`/`SessionAlreadyCompleteError`/
        `SessionNotResumableError`) if its own validation -- which runs
        *before* it ever calls `ui.session_started()` -- rejects the
        session. `app.py` maps those the same way `cli.py`'s `resume`
        command already maps them (exit code 3 -> HTTP 404/409): single
        owner for resumability logic is `Orchestrator.resume()` itself,
        this method only waits to observe its outcome rather than
        re-deriving the same checks.
        """
        store = SessionStore(self._config.session_db_path)
        ui = WebSessionUI()
        orchestrator = Orchestrator(config=self._config, session_store=store, ui=ui)
        error_holder: dict[str, BaseException] = {}
        thread_done = threading.Event()

        def _run() -> None:
            try:
                orchestrator.resume(session_id)
            except BaseException as exc:  # noqa: BLE001 - see start_session's _run for the same reasoning
                error_holder["error"] = exc
                ui.error(str(exc))
            finally:
                store.close()
                thread_done.set()

        thread = threading.Thread(
            target=_run, name=f"viva-session-resume-{session_id}", daemon=True
        )
        thread.start()

        self._await_session_id(ui, thread_done, error_holder, expected_session_id=session_id)
        with self._lock:
            self._sessions[session_id] = LiveSession(ui=ui, thread=thread, store=store)
        return ui

    def get(self, session_id: str) -> WebSessionUI | None:
        with self._lock:
            live = self._sessions.get(session_id)
        return live.ui if live else None

    def shutdown(self) -> None:
        """Called once, on server shutdown -- unsticks any thread still
        blocked in read_answer() so the process can exit rather than hang
        on a session nobody's answering anymore."""
        with self._lock:
            live_sessions = list(self._sessions.values())
        for live in live_sessions:
            live.ui.request_shutdown()

    @staticmethod
    def _await_session_id(
        ui: WebSessionUI,
        thread_done: threading.Event,
        error_holder: dict[str, BaseException],
        expected_session_id: str | None = None,
    ) -> str:
        deadline = time.monotonic() + _SESSION_ID_WAIT_SECONDS
        while time.monotonic() < deadline:
            if ui.id_ready or thread_done.is_set():
                break
            time.sleep(_POLL_INTERVAL_SECONDS)

        if not ui.id_ready:
            # Failed before ever getting a session_id -- e.g. resume()'s
            # own not-found/not-resumable checks, or (rare) a SessionStore
            # failure on session creation. Nothing to register or return;
            # re-raise so the caller (an HTTP route) can map the specific
            # exception type to a status code.
            if "error" in error_holder:
                raise error_holder["error"]
            raise OrchestratorError(
                "Timed out waiting for the session to start -- this points at a "
                "SessionStore/config problem, not a slow LLM call (session "
                "creation never touches the model)."
            )
        return expected_session_id or ui.session_id  # type: ignore[return-value]
