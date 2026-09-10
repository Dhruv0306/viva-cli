"""FastAPI app for `viva serve` (docs/plan.md Phase 10, docs/system-design/
15-phase-10-web-ui-design.md \u00a715.5).

Mirrors the CLI contract (docs/system-design/
06-cli-contract-and-profile-scaling.md \u00a76.1) exactly -- start/resume/
list/report/cleanup, plus the live question/answer loop -- by calling
straight into the same `Orchestrator` (via `SessionRegistry`),
`SessionStore`, `ReportBuilder`, and `run_cleanup` the CLI commands
(`cli.py`) already use. No pipeline logic lives here (design doc \u00a73.7):
every route either delegates to those, or to `SessionRegistry`
(`registry.py`) for the live-session thread bridge.

CLI exit codes map to HTTP statuses the same way throughout: 2 (bad
input) -> 400, 3 (not found / wrong state) -> 404/409, 1 (unexpected)
-> 500.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from viva.cleanup import run_cleanup
from viva.config import Config
from viva.indexer.store import VectorStore
from viva.orchestrator import (
    OrchestratorError,
    SessionAlreadyCompleteError,
    SessionNotFoundError,
    SessionNotResumableError,
    is_resumable,
)
from viva.report import ReportBuilder, render_html, render_json, render_markdown
from viva.storage import SessionStore
from viva.voice_io import VoiceDependencyError
from viva.web.registry import SessionRegistry
from viva.web.web_session_ui import STAGE_AWAITING_ANSWER

_STATIC_DIR = Path(__file__).parent / "static"


class StartSessionRequest(BaseModel):
    repo_url: str
    branch: str | None = None
    duration_minutes: int | None = None
    session_name: str | None = None


class AnswerRequest(BaseModel):
    text: str


class CleanupRequest(BaseModel):
    older_than: int | None = None
    all: bool = False


def create_app(config: Config) -> FastAPI:
    registry = SessionRegistry(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        registry.shutdown()

    app = FastAPI(title="viva room", lifespan=lifespan)

    # -- live session lifecycle (start/resume/state/answer) --------------------

    @app.post("/api/sessions")
    def start_session(body: StartSessionRequest) -> dict:
        try:
            session_id = registry.start_session(
                body.repo_url, branch=body.branch,
                duration_minutes=body.duration_minutes, session_name=body.session_name,
            )
        except Exception as exc:  # noqa: BLE001 - registry.start_session() only raises here for a failure *before* a session_id exists (SessionStore/config problem), the same class of thing cli.py's `start` command maps to exit code 1
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"session_id": session_id}

    @app.post("/api/sessions/{session_id}/resume")
    def resume_session(session_id: str) -> dict:
        try:
            registry.resume_session(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (SessionAlreadyCompleteError, SessionNotResumableError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - anything else is unexpected, same as cli.py resume's uncaught-exception -> exit 1 path
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"session_id": session_id}

    @app.get("/api/sessions/{session_id}/state")
    def session_state(session_id: str) -> dict:
        ui = registry.get(session_id)
        if ui is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "No live session with this id in this server process. "
                    "If it already finished, GET its /report; otherwise "
                    "POST /resume to continue it."
                ),
            )
        return ui.snapshot()

    @app.post("/api/sessions/{session_id}/answer")
    def submit_answer(session_id: str, body: AnswerRequest) -> dict:
        ui = registry.get(session_id)
        if ui is None:
            raise HTTPException(status_code=404, detail="No live session with this id.")
        if not ui.submit_answer(body.text):
            raise HTTPException(
                status_code=409, detail="Session is not currently awaiting an answer.",
            )
        return {"status": "recorded"}

    # -- voice (Phase 12, docs/system-design/17-phase-12-web-voice-io-design.md) --

    @app.get("/api/voice/available")
    def voice_available() -> dict:
        # Plain `def`, same as every other route in this file except
        # answer_audio below -- Starlette offloads this to its threadpool
        # automatically, so the (possibly several-second, first-call-only)
        # model load this can trigger doesn't block the event loop.
        available, reason = registry.voice_available()
        return {
            "available": available,
            "reason": reason,
            # Mirrors the CLI's own recording cutoff (design doc §16.3)
            # so the frontend's AudioWorklet-based silence detection
            # (static/voice-worklet.js) uses the server's actual
            # configured values rather than separately hardcoded
            # constants that could drift out of sync with them.
            "max_answer_seconds": config.voice_max_answer_seconds,
            "silence_timeout_seconds": config.voice_silence_timeout_seconds,
        }

    @app.get("/api/sessions/{session_id}/question-audio")
    def question_audio(session_id: str) -> Response:
        ui = registry.get(session_id)
        if ui is None:
            raise HTTPException(status_code=404, detail="No live session with this id.")
        snapshot = ui.snapshot()
        if snapshot["stage"] != STAGE_AWAITING_ANSWER or not snapshot["question_text"]:
            raise HTTPException(status_code=409, detail="No current question to speak.")
        try:
            audio = registry.synthesize(snapshot["question_text"])
        except VoiceDependencyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(content=audio, media_type="audio/wav")

    @app.post("/api/sessions/{session_id}/answer-audio")
    async def answer_audio(session_id: str, request: Request) -> dict:
        # The one genuinely `async def` route in this file -- reading a
        # raw (non-JSON) request body needs `await request.body()`,
        # which only a coroutine route can call. registry.transcribe()
        # itself is still blocking (a real faster-whisper call under a
        # lock), so it's explicitly handed to Starlette's threadpool via
        # run_in_threadpool() rather than awaited directly -- awaiting a
        # blocking call here would stall the asyncio event loop for
        # every other concurrent request (every other route in this file
        # gets that offloading for free from being a plain `def`).
        #
        # Deliberately does NOT call ui.submit_answer() itself (a real-
        # world UX gap found in testing, design doc §17.8): the person
        # can't see or correct a misheard word before it's already been
        # submitted if this both transcribes and submits in one step.
        # Returns the transcribed text; the frontend puts it in the
        # answer textarea for review/editing, and the existing
        # POST .../answer route is what actually submits it, same as a
        # typed answer.
        ui = registry.get(session_id)
        if ui is None:
            raise HTTPException(status_code=404, detail="No live session with this id.")
        pcm = await request.body()
        try:
            text = await run_in_threadpool(registry.transcribe, pcm, ui.timer)
        except VoiceDependencyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not text:
            raise HTTPException(status_code=422, detail="Could not transcribe any speech.")
        return {"text": text}

    # -- list/report/cleanup: read straight from SessionStore, same as CLI -----

    @app.get("/api/sessions")
    def list_sessions(status: str | None = None) -> list[dict]:
        store = SessionStore(config.session_db_path)
        try:
            sessions = store.list_sessions(status)
        finally:
            store.close()
        # is_resumable() mirrors Orchestrator.resume()'s own validation
        # (orchestrator.py) -- without it, the frontend previously had
        # to guess at resumability from the raw status string and
        # offered a "Resume" button for sessions resume() would always
        # 409 on (interrupted before the live Q&A session began; see
        # design doc \u00a715.14).
        return [{**asdict(s), "resumable": is_resumable(s.status)} for s in sessions]

    @app.get("/api/sessions/{session_id}/report")
    def report(session_id: str, format: str = "md", allow_partial: bool = False, download: bool = False):
        if format not in ("md", "json", "html"):
            raise HTTPException(status_code=400, detail="format must be 'md', 'json', or 'html'")

        store = SessionStore(config.session_db_path)
        try:
            session = store.get_session(session_id)
            if session is None:
                raise HTTPException(
                    status_code=404, detail=f"No session found with id {session_id!r}.",
                )
            if session.status != "COMPLETE" and not allow_partial:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Session {session_id!r} is not COMPLETE (status: "
                        f"{session.status}). Pass allow_partial=true to view a "
                        "report anyway."
                    ),
                )
            qa_records = store.get_qa_records(session_id)
        finally:
            store.close()

        built_report = ReportBuilder().build(
            session, qa_records, max_items_per_section=config.report_max_items_per_section,
        )
        if format == "json":
            text, media_type, ext = render_json(built_report), "application/json", "json"
        elif format == "html":
            text, media_type, ext = render_html(built_report), "text/html", "html"
        else:
            text, media_type, ext = render_markdown(built_report), "text/markdown", "md"

        headers = None
        if download:
            # Triggers an actual file save instead of the browser
            # rendering/navigating to the response inline -- what the
            # web UI's "Download .md"/"Download .json" buttons link
            # straight at (static/app.js). Not offered for format=html,
            # which is only ever fetched for on-screen rendering.
            slug = (session.repo_slug or session.session_id).replace("/", "-")
            headers = {"Content-Disposition": f'attachment; filename="report-{slug}.{ext}"'}
        return PlainTextResponse(content=text, media_type=media_type, headers=headers)

    @app.post("/api/cleanup")
    def cleanup(body: CleanupRequest) -> dict:
        if body.older_than is not None and body.older_than <= 0:
            raise HTTPException(
                status_code=400, detail=f"older_than must be positive, got {body.older_than}",
            )
        retention_days = body.older_than if body.older_than is not None else config.session_retention_days

        store = SessionStore(config.session_db_path)
        try:
            result = run_cleanup(
                store, VectorStore(config.vector_db_path),
                older_than_days=retention_days, purge_all=body.all,
            )
        except Exception as exc:  # noqa: BLE001 - mirrors cli.py cleanup's uncaught-exception -> exit 1 path
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            store.close()
        return {**asdict(result), "is_empty": result.is_empty}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        # Browsers request /favicon.ico directly on first load, regardless
        # of the <link rel="icon"> tag in index.html (which points at
        # /static/favicon.svg) -- without this route that request 404s
        # even though the page itself renders fine, exactly what showed
        # up in a real `viva serve` run's log (this session). Serving the
        # same SVG at the literal /favicon.ico path (with an explicit
        # media_type, since FileResponse would otherwise guess one from
        # the .ico extension) works in every current browser -- none of
        # them actually require the legacy ICO binary format.
        return FileResponse(_STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    # index.html references its assets as absolute /static/... paths
    # (static/index.html), so the mount point has to be /static, not /
    # -- mounting a StaticFiles instance at "/" with html=True does serve
    # index.html at "/" too, but it does *not* also make style.css/app.js
    # reachable at /static/style.css and /static/app.js; there's no
    # /static prefix registered anywhere in that setup, so those 404
    # (as reported: 200 on GET /, 404 on GET /static/style.css and
    # /static/app.js -- the browser loading index.html successfully and
    # then failing to fetch the assets it references is exactly what a
    # missing /static mount looks like). Mounted after the /api/* and /
    # routes above -- API routes always match first regardless of mount
    # order, but this keeps the "most specific first" reading order.
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    return app
