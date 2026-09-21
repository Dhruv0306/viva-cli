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

import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from viva.cleanup import run_cleanup
from viva.config import Config
from viva.indexer.store import VectorStore
from viva.ingest.clone import CloneError
from viva.orchestrator import (
    InvalidParametersError,
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

# docs/system-design/19-panel-review-findings-2026-09.md §19.4.1 /
# docs/system-design/21-phase-15-serve-authentication-design.md §21.3 --
# an explicit allowlist, not a "looks private" heuristic. Anything not in
# this set (0.0.0.0, a real LAN IP, ...) requires a token; fail-open-to-
# requiring-auth is the safer direction if this list is ever incomplete.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _requires_auth(host: str) -> bool:
    return host not in _LOOPBACK_HOSTS


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


def create_app(config: Config, host: str = "127.0.0.1") -> FastAPI:
    registry = SessionRegistry(config)
    # §21.3/§21.4 -- every existing create_app(config) call site (9 of
    # them, all in test_web_app.py) omits `host`, so this defaults to the
    # loopback address and require_token/token below evaluate to
    # False/None exactly as before this phase existed. Only a real,
    # non-loopback `host` (as passed by cli.py's `serve` command) turns
    # auth on at all.
    require_token = _requires_auth(host)
    token = secrets.token_urlsafe(24) if require_token else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        registry.shutdown()

    app = FastAPI(title="viva room", lifespan=lifespan)
    app.state.viva_token = token

    # -- live session lifecycle (start/resume/state/answer) --------------------


    @app.post("/api/sessions")
    def start_session(body: StartSessionRequest) -> dict:
        try:
            session_id = registry.start_session(
                body.repo_url, branch=body.branch,
                duration_minutes=body.duration_minutes, session_name=body.session_name,
            )
        except (CloneError, InvalidParametersError) as exc:
            # A malformed repo_url or a non-positive duration_minutes --
            # rejected by Orchestrator.start() before a session_id ever
            # exists, same as cli.py's `start` command maps these to
            # exit code 2. See docs/system-design/19-panel-review-
            # findings-2026-09.md §19.3.1/§19.5.2 and docs/system-design/
            # 20-phase-14-security-hardening-design.md §20.3.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - any other failure before a session_id exists (SessionStore/config problem) is unexpected, the same class of thing cli.py's `start` command maps to exit code 1
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
    def index(request: Request) -> Response:
        # §21.6 -- no templating engine in this codebase (§15.2's "no new
        # frontend toolchain"), so this is a plain string substitution
        # against a placeholder already in index.html, not a Jinja
        # render. When token is None (the loopback/default case, by far
        # the common one) this substitutes an empty string --
        # `window.__VIVA_TOKEN__ = "";` -- so the page renders byte-for-
        # byte the same as it always has for anyone not opting into a
        # non-loopback bind.
        #
        # Real-world testing caught a serious bug in an earlier version
        # of this route: it embedded the real `token` unconditionally on
        # every visit to `/`, regardless of whether the visitor had
        # supplied it. Since `/` is deliberately left unauthenticated
        # (§21.5 -- the browser has to load the page before any
        # JS-driven auth can run), that meant anyone could load the bare
        # URL with no token at all, read the real secret straight out of
        # the page source, and use it for every /api/* call from then
        # on -- the ?token= requirement on the printed link was
        # cosmetic, not enforced. The fix: only embed the real token
        # when *this* request already proves it, via the same query
        # param the /api/* middleware accepts. A request to `/` with no
        # token, or the wrong one, gets an empty string embedded, same
        # as the loopback case -- its page's JS calls will correctly
        # 401 against /api/*, matching the actual security boundary.
        embed_token = token if (not require_token or request.query_params.get("token") == token) else None
        html = (_STATIC_DIR / "index.html").read_text()
        html = html.replace("{{VIVA_TOKEN}}", embed_token or "")
        return Response(content=html, media_type="text/html")

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

    # §19.4.1/§21.5 -- required on /api/* only, and only when bound to a
    # non-loopback address (require_token is False for the default case,
    # making this a single closed-over boolean check per request with no
    # measurable cost added to the common path). / and /static/* stay
    # reachable without a token even on a non-loopback bind: the browser
    # has to successfully load index.html and app.js before any
    # JS-driven auth can run at all, and neither route exposes session
    # data -- only /api/* does, which is the boundary that matters.
    @app.middleware("http")
    async def _require_token(request: Request, call_next):
        if not require_token or not request.url.path.startswith("/api/"):
            return await call_next(request)
        supplied = request.headers.get("x-viva-token") or request.query_params.get("token")
        if supplied != token:
            return JSONResponse(status_code=401, content={"detail": "Missing or invalid access token."})
        return await call_next(request)

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
