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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from viva.cleanup import run_cleanup
from viva.config import Config
from viva.indexer.store import VectorStore
from viva.orchestrator import (
    OrchestratorError,
    SessionAlreadyCompleteError,
    SessionNotFoundError,
    SessionNotResumableError,
)
from viva.report import ReportBuilder, render_json, render_markdown
from viva.storage import SessionStore
from viva.web.registry import SessionRegistry

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

    app = FastAPI(title="viva-web", lifespan=lifespan)

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

    # -- list/report/cleanup: read straight from SessionStore, same as CLI -----

    @app.get("/api/sessions")
    def list_sessions(status: str | None = None) -> list[dict]:
        store = SessionStore(config.session_db_path)
        try:
            sessions = store.list_sessions(status)
        finally:
            store.close()
        return [asdict(s) for s in sessions]

    @app.get("/api/sessions/{session_id}/report")
    def report(session_id: str, format: str = "md", allow_partial: bool = False):
        if format not in ("md", "json"):
            raise HTTPException(status_code=400, detail="format must be 'md' or 'json'")

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
            return PlainTextResponse(content=render_json(built_report), media_type="application/json")
        return PlainTextResponse(content=render_markdown(built_report), media_type="text/markdown")

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
