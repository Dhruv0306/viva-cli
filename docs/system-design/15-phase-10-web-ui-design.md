# System Design Reference — Part 15: Phase 10 Web UI Design

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts.

**Naming:** the browser interface `viva serve` runs is called **viva-web**
throughout this doc, the app's page title/header, and the FastAPI app's
own `title=`. `viva-cli` remains the name of the whole project/package;
`viva-web` is what a person sees on screen once they're in the browser,
the same way `viva start`/`viva report`/etc. are commands *within*
`viva-cli` rather than separate products.

## 15.1 Scope and renumbering

`docs/plan.md`'s Phase 9 entry ends with "Stretch: simple web UI."
`14-phase-9-polish-design.md` §14.1 audited Phase 9 against the merged
codebase and explicitly deferred the web UI: "stretch, explicitly out of
scope per `plan.md` and reconfirmed out of scope by
`13-phase-8-report-design.md` §13.10. Not attempted here." Phase 9 is
fully merged (`viva cleanup`, NFR7) — there is no open Phase 9 branch to
add this to.

This doc treats the web UI as **Phase 10**, not a reopened Phase 9,
consistent with the project's own phase-is-closed-once-merged discipline
(e.g. `04-open-questions.md` item 4 being marked resolved rather than
rewritten in place). It picks up exactly where the plan's stretch item
left off — nothing here overrides `13-phase-8-report-design.md` or
`14-phase-9-polish-design.md`.

This is *not* a surprise addition to the architecture: `03-final-
architecture.md` §3.1 already draws `CLI / UI (Typer+Rich CLI first;
FastAPI web UI later — same core underneath)`, §3.7 already says "CLI and
future web UI both sit on top of the same Orchestrator — no pipeline
logic lives in the interface layer," and `requirements.md` §4 already
lists "CLI is the primary interface for v1; a web UI is an explicit
future phase." Phase 10 is that future phase arriving.

## 15.2 What "simple" means here

Per the user's framing and the project's own minimalism bias (no new
abstractions where an existing seam already does the job), "simple" is
scoped deliberately narrow:

- One local server process (`viva serve`), one browser tab, one session
  at a time — matching `requirements.md` §4's existing "single user,
  single active session at a time (v1)" assumption. The web UI does not
  relax this; it's a second *interface* onto the same constraint, not a
  reason to add multi-session concurrency control.
- Server-rendered HTML + vanilla JS polling, not a JS framework/build
  step/SPA. No new frontend toolchain (no npm, no bundler) — the whole
  point of "simple" is that `pip install -e .` still gets you everything.
- No auth, no HTTPS, binds to `127.0.0.1` by default. This is a local
  tool for one developer's own use, same trust boundary the CLI already
  has (whoever can run `viva start` on this machine can already read/
  write all of its data). Not a hardened multi-user service.
- Feature parity with the existing CLI contract (`06-cli-contract-and-
  profile-scaling.md` §6.1) — `start`, `resume`, `list`, `report`,
  `cleanup` — plus the live session loop. No new capability the CLI
  doesn't already have.

## 15.3 The one real architectural problem

Everything else in this doc is routine FastAPI wiring. This section is
the part that actually needs a design decision.

`Orchestrator.start()` and `Orchestrator.resume()` (`orchestrator.py`)
each run the *entire* pipeline synchronously to completion in one call —
`INGESTING` through `COMPLETE` — including the live `IN_PROGRESS` loop,
which blocks on `SessionUI.read_answer()` (`session_ui.py`) once per
question. `RichSessionUI.read_answer()` blocks on a real terminal
(`prompt_toolkit.PromptSession.prompt()`). That's the correct model for a
CLI process talking to its own controlling terminal. It is not a request/
response model, and an HTTP endpoint cannot block for however many
minutes it takes a person to type an answer.

**Decision: a queue-backed `WebSessionUI`, with the Orchestrator call
running on a background thread — not an Orchestrator rewrite.**

`SessionUI` is already an ABC (`session_ui.py`) that `orchestrator.py`
only ever talks to through `self.ui` — the same seam that let Phase 6
test the session loop with a scripted fake UI instead of a real TTY.
Phase 10 adds a second real implementation, `WebSessionUI`, rather than
touching `Orchestrator` or the existing `RichSessionUI`:

```python
class WebSessionUI(SessionUI):
    """Bridges the Orchestrator's blocking calls to HTTP request/response.

    One instance per live session. `ask_question`/`stage_started`/etc.
    write into a small in-memory state snapshot the polling endpoint
    reads; `read_answer` blocks on a `queue.Queue` that the answer-
    submission endpoint pushes into. The Orchestrator thread and the
    FastAPI request threads never touch each other directly -- only
    through this queue and the snapshot, both guarded by one lock.
    """
    def __init__(self) -> None:
        self._answer_queue: queue.Queue[str] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._state = WebSessionState(stage="starting")

    def ask_question(self, question_text, category, question_number) -> None:
        with self._lock:
            self._state = replace(self._state, stage="awaiting_answer",
                                   question_text=question_text, category=category,
                                   question_number=question_number)

    def read_answer(self, timer: AnswerTimer) -> str:
        # Blocks the Orchestrator's background thread only -- never an
        # HTTP request thread. Times out on its own poll interval so a
        # server-side shutdown can still interrupt it; the *user's*
        # timer semantics (FR17) are unchanged, since `timer.expired()`
        # is still what `_run_live_session` checks, not this call.
        while True:
            try:
                return self._answer_queue.get(timeout=0.5)
            except queue.Empty:
                if self._shutdown.is_set():
                    return ""

    def submit_answer(self, text: str) -> None:
        self._answer_queue.put(text)

    def snapshot(self) -> WebSessionState:
        with self._lock:
            return self._state
```

`Orchestrator.start(...)` / `.resume(...)` run exactly as they do today,
just inside `threading.Thread(target=orchestrator.start, ...)` instead of
directly inside a Typer command function. The Orchestrator, `SessionStore`,
and `VectorStore` are all already safe to call from a single background
thread this way — nothing in Phase 0–9 assumed "the caller is the main
thread," only that there's exactly one caller per session at a time
(`03-final-architecture.md` §3.8), which Phase 10 preserves.

This keeps the "no pipeline logic in the interface layer" rule
(§3.7) intact: `WebSessionUI` is pure plumbing (a queue and a lock), the
same category of thing `RichSessionUI` already is — neither knows
anything about ingestion, RAG, or evaluation.

## 15.4 Module boundary: `src/viva/web/`

New package, isolated the same way `report.py` and `cleanup.py` are kept
out of `orchestrator.py`/`cli.py`:

```
src/viva/web/
  __init__.py
  app.py            # FastAPI() instance, route handlers
  web_session_ui.py # WebSessionUI, WebSessionState (§15.3)
  registry.py       # in-memory dict[session_id, WebSessionUI + thread handle]
  static/
    index.html       # single page: start form + list + live session + report view
    app.js            # fetch()-based polling, no framework
    style.css
```

`registry.py` exists because a live `WebSessionUI` and its background
thread are process-local state that doesn't belong in `SessionStore` —
`SessionStore` already durably persists everything the *session* needs to
survive a restart (that's what makes `viva resume` work); the registry
only tracks *which sessions currently have a live thread running in this
process*. If the server restarts mid-session, the registry is empty on
boot — the session simply isn't "live" anymore, and the existing `viva
resume` semantics (re-derived from `SessionStore`, per `11-phase-6-
session-loop-design.md`) are what bring it back, exactly as they already
do for the CLI today. No new persistence mechanism.

## 15.5 API surface (mirrors the CLI contract, `06-cli-contract...md` §6.1)

| Endpoint | CLI equivalent | Notes |
|---|---|---|
| `POST /api/sessions` `{repo_url, branch?, duration?, session_name?}` | `viva start` | Returns `{session_id}` immediately (same "print session_id right away" guarantee as the CLI, §6.1), spawns the background thread, client then polls state |
| `POST /api/sessions/{id}/resume` | `viva resume` | Spawns a fresh background thread bound to the existing `session_id` |
| `GET /api/sessions/{id}/state` | (live terminal output) | Polled by the browser; returns `WebSessionState` — stage, current question, time remaining, last-answer-recorded flag |
| `POST /api/sessions/{id}/answer` `{text}` | (typing + Alt+Enter) | Feeds `WebSessionUI.submit_answer`; 409 if the session isn't currently `awaiting_answer` |
| `GET /api/sessions` `?status=` | `viva list` | Reads `SessionStore.list_sessions` directly, same as the CLI command does — no Orchestrator involved, matching `14-phase-9...md` §14.4's "list_sessions already reads SessionStore directly" precedent |
| `GET /api/sessions/{id}/report` `?format=&allow_partial=` | `viva report` | Calls `ReportBuilder.build` + `render_markdown`/`render_json` (`report.py`) directly; returns Markdown as `text/markdown` or the JSON body, not re-implemented |
| `POST /api/cleanup` `{older_than?, all?}` | `viva cleanup` | Calls `run_cleanup` (`cleanup.py`) directly, returns `CleanupReport` as JSON |

**Exit-code → HTTP-status mapping**, since every one of these wraps a call
that already raises the CLI's typed exceptions (`OrchestratorError` and
subclasses, `CloneError`, `ConfigError`):

| CLI exit code | Meaning | HTTP status |
|---|---|---|
| `0` | success | `200` |
| `1` | unexpected/internal error | `500` |
| `2` | invalid input (bad URL, bad config) | `400` |
| `3` | valid input, not actionable (not found, already complete, not yet complete without `--allow-partial`) | `404` for not-found, `409` for wrong-state |

No new error taxonomy — this is a translation layer over the exceptions
`cli.py` already catches, per §3.7's "no pipeline logic in the interface
layer": the web layer doesn't decide what's an error, it just reports
what the Orchestrator/`ReportBuilder`/`run_cleanup` already decided.

## 15.6 Frontend

Single static page, three views swapped by a tiny bit of JS (no router
library — `if`/`else` on which section is visible is enough for three
views):

1. **Start/list view** — a form (repo URL, branch, duration,
   session-name) posting to `POST /api/sessions`, and a table from
   `GET /api/sessions` with a "Resume" link per non-`COMPLETE` row.
2. **Live session view** — polls `GET /api/sessions/{id}/state` every
   2s (deliberately coarser than `RichSessionUI`'s 0.5s terminal
   refresh — a browser poll isn't rendering a smooth countdown widget,
   just needs to feel responsive; the timer display itself computes the
   visible countdown client-side between polls from the
   `remaining_seconds` + a timestamp, the same "don't hammer the server
   for a cosmetic tick" tradeoff `format_remaining()`'s MM:SS granularity
   already makes for the terminal). Shows the current question, a
   `<textarea>`, and a submit button that posts to
   `/api/sessions/{id}/answer`.
3. **Report view** — fetches `GET /api/sessions/{id}/report` and renders
   the Markdown (a small client-side Markdown-to-HTML pass, or just
   `<pre>` — Markdown rendered as preformatted text is a legitimate
   "simple" choice and avoids pulling in a client-side Markdown library
   for a v1 stretch feature) with a link to the raw `?format=json`.

No WebSockets/SSE (§15.9) — the whole surface is `fetch()` calls against
plain JSON/text endpoints, servable as static files with zero build step.

## 15.7 CLI entry point

```
viva serve [--host <addr>] [--port <n>]
```

Defaults `127.0.0.1:8000` per §15.2. Follows the same `Config.load()` →
catch `ConfigError` → exit 2 pattern every other command in `cli.py`
already uses, then hands off to `uvicorn.run(app, host=..., port=...)`.
Not a daemon/background service — same "runs until you Ctrl+C it"
posture as `viva start` already has; no new process-management story.

## 15.8 New dependencies

```
fastapi>=0.115,<1.0
uvicorn>=0.30,<1.0
```

No new frontend toolchain (§15.2). No new LLM/storage dependency —
`src/viva/web/` calls straight into the existing `Orchestrator`,
`SessionStore`, `VectorStore`, `ReportBuilder`, and `run_cleanup`.

## 15.9 Explicitly out of scope

- **WebSockets/SSE for live updates.** Polling is simpler, has no new
  dependency, and is adequate at a 2s cadence for a single local user —
  matches §15.2's "simple" framing. Worth revisiting only if polling
  proves visibly laggy in real use.
- **Multi-session-at-once web UI** (e.g. running two vivas in parallel
  from two browser tabs). `requirements.md` §4's single-active-session
  assumption is a v1 constraint the web UI inherits, not one this phase
  should quietly lift by giving the registry multi-thread capacity it
  wasn't asked to have.
- **Auth, HTTPS, non-localhost binding by default.** Same trust model as
  the CLI (§15.2); `--host 0.0.0.0` remains possible for someone who
  explicitly wants it, but isn't the default and isn't hardened for it.
- **Any change to `Orchestrator`, `SessionUI`'s abstract contract, or
  `RichSessionUI`.** Phase 10 adds a second implementation; it doesn't
  touch the first one or the interface it implements.
- **Voice/real-time input** — already out of scope per `requirements.md`
  §5 and unrelated to this phase.

## 15.10 Testing

- `WebSessionUI` unit tests: push/pop the queue directly, no real thread,
  no FastAPI — same "test the plumbing in isolation" posture
  `test_session_ui.py` already applies to `RichSessionUI` with a fake
  `Console`.
- `tests/test_web_app.py`: FastAPI's `TestClient` against `app.py`, with
  the Orchestrator's LLM/embedding/classification dependencies swapped
  for the same fakes/`NullClassificationProvider` (`classification.py`)
  the rest of the suite already uses — no real Ollama calls in CI, same
  constraint every other test file already honors.
- One real end-to-end manual pass against an actual local Ollama +
  browser session before calling this phase done — consistent with
  "Real-world testing is non-negotiable" and the fact that the queue/
  thread bridge in §15.3 is genuinely new mechanism, not a restatement
  of something already proven like `14-phase-9...md`'s cleanup work was.

## 15.11 Migration / blast radius

- `src/viva/web/` — new package (§15.4).
- `src/viva/cli.py` — new `serve` command (§15.7). No changes to
  existing commands.
- `pyproject.toml` — add `fastapi`, `uvicorn` to `dependencies`
  (§15.8); optionally an `[project.optional-dependencies.web]` extra
  instead, if keeping the base CLI install free of a web framework for
  people who never run `viva serve` is preferred — **open question,
  needs a decision before implementation** (§15.12).
- `docs/plan.md` — Phase 9's "Stretch: simple web UI" line should be
  struck through and point here, same pattern `04-open-questions.md`
  uses for resolved items; a new "Phase 10 — Web UI" entry added.
- `README.md` — new `viva serve` usage section, "Phases 0-10" status
  line once merged.
- New tests: `tests/test_web_session_ui.py`, `tests/test_web_app.py`,
  `tests/test_cli_serve.py` (smoke test only — starting a real uvicorn
  server in a CLI test is more than that command needs to prove; a
  `--help`/config-error-exit-2 check is consistent with how thin
  `cli.py`'s other command bodies are meant to be).

## 15.12 Decisions on the previously-open questions

The three items this doc originally flagged as open (§15.12, pre-
implementation) are settled as follows, so implementation isn't blocked
on them. Revisit if real usage disagrees:

1. **Base dependency, not a `[web]` extra.** `fastapi`/`uvicorn` are
   added to `pyproject.toml`'s unconditional `dependencies` (§15.8).
   The project has no existing precedent for an optional-extras split
   (only `[dev]` exists, which is a different axis — contributor tooling
   vs. runtime), and introducing one for a single command adds packaging
   complexity (two more CI matrix legs, `pip install -e .` vs.
   `.[web]` in every doc/README example) disproportionate to the actual
   install-size cost of two libraries. `cli.py`'s `serve` command still
   imports `uvicorn`/`viva.web.app` lazily inside the function body
   (§15.7 note) so the cost of these deps is only ever paid by someone
   who actually runs `viva serve`, not by every CLI invocation's import
   time — the practical concern behind the original question — without
   needing a packaging-level split to get there.
2. **2-second poll interval, kept as the starting value.** Still
   unvalidated against real usage (that requires the real-world-testing
   pass §15.10 already calls for), but it's a client-side constant
   (`static/app.js`) that costs nothing to tune later without touching
   the server contract — not worth blocking implementation on.
3. **Report view renders Markdown as `<pre>` plaintext.** Zero new
   frontend dependency, consistent with §15.2's "no new frontend
   toolchain" framing; a `?format=json`/raw-Markdown-download link sits
   alongside it for anyone who wants it rendered properly in their own
   editor.

## 15.13 Real-world bugs found during Phase 10 testing

**Static assets 404'd even though the page itself loaded.** Found
running a real `viva serve` on Windows (Dhruv's machine, this session):
`GET /` returned `200`, but `GET /static/style.css` and
`GET /static/app.js` both returned `404` -- the page loaded unstyled and
non-interactive. Root cause: `app.py` originally mounted the static
directory with `app.mount("/", StaticFiles(directory=..., html=True),
name="static")`. `html=True` does make that mount serve `index.html` at
`/`, which is why `GET /` worked -- but it does not also register a
`/static` prefix anywhere. `index.html` itself has always referenced its
assets as absolute `/static/style.css` and `/static/app.js` paths (see
`static/index.html`, unchanged since it was written) -- there was simply
no mount point matching those paths at all, a path-prefix mismatch
between where the assets were *mounted* and where the page *asked for*
them, not a missing-file problem.

Fixed: `app.py` now mounts `StaticFiles(directory=..., html=False)` at
`/static` (matching what `index.html` has always asked for), and adds a
dedicated `GET /` route returning `FileResponse(_STATIC_DIR /
"index.html")` explicitly, rather than relying on `StaticFiles`'
`html=True` fallback to cover the root path implicitly.

Regression test:
`test_static_assets_referenced_by_index_html_are_served` in
`tests/test_web_app.py`, added and confirmed to fail against the
pre-fix code first (`404` on both asset requests, reproducing the
real terminal output exactly), then confirmed to pass against the fix.
`test_root_serves_index_html` (already existing) only ever checked
`GET /` itself, which is exactly why it didn't catch this -- it passed
on both the buggy and fixed code, since `GET /`'s behavior never
actually changed.

**"Resume" offered for sessions that could only ever 409.** Found from
a real `viva serve` log: repeated `POST /sessions/{id}/resume` ->
`409 Conflict` on the same session, every time it was clicked. Root
cause: `app.js`'s session list decided whether to show a "Resume"
button purely from `status != COMPLETE && status != FAILED` --
but `Orchestrator.resume()` also refuses a session interrupted during
`INGESTING`/`ANALYZING`/`INDEXING`/`PLANNING` (crashed before the live
Q&A session ever began -- `11-phase-6-session-loop-design.md`'s "Known
limitations", unchanged, Phase 6 behavior). A session whose local LLM
analysis got interrupted -- exactly the kind of thing more likely with
a bigger repo -- sat in the sessions list with a "Resume" button that
was guaranteed to fail no matter how many times it was clicked, and the
list never explained why.

Fixed at the source rather than re-deriving the same status list a
second time in JS: `orchestrator.py` gained `is_resumable(status)`, a
public function mirroring exactly what `resume()`'s own validation
accepts (the pre-session-statuses constant it checks against was
renamed from `_NOT_RESUMABLE_PRE_SESSION_STATUSES` to
`NOT_RESUMABLE_PRE_SESSION_STATUSES` to make it a legitimate
cross-module dependency rather than reaching into a private name).
`GET /api/sessions` now includes a server-computed `resumable` field
per session, and `app.js` uses that instead of guessing -- a session
that can't be resumed shows its status with no action button at all,
rather than one that's guaranteed to fail.

Regression tests: `test_is_resumable_matches_what_resume_itself_accepts`
in `test_orchestrator.py` (11 parametrized statuses, each checked
against both `is_resumable()` and an actual `orch.resume()` call, so
the two can't silently drift apart again) and
`test_list_sessions_resumable_field_matches_orchestrator_validation`
in `test_web_app.py` (confirmed to fail against the pre-fix `app.py`
first with a `KeyError` on the missing field, then pass against the
fix).

**Countdown kept ticking after the answer was already submitted.**
Reported from real use: the on-screen clock didn't stop once a
question was answered, and kept counting down through the gap before
the next question appeared. The backend's actual time budget was
never wrong -- `AnswerTimer.excluding()` (`timer.py`) already excludes
classification/question-generation LLM time from what counts against
the person -- but two things surfaced that gap as if it still counted:
`WebSessionUI.snapshot()` returned a live `timer.remaining()` read
regardless of stage, which (since `excluding()` only subtracts elapsed
time back out once its block *finishes*, not while one is still in
flight) could visibly tick down before self-correcting; and
`static/app.js`'s client-side `tickTimer_()` interpolated the
displayed number every second unconditionally, with no notion of
"stage" at all, so it kept ticking straight through the "processing"
gap between polls too.

Fixed at both layers: `WebSessionUI` now freezes `remaining_seconds`
at the instant an answer is recorded (`read_answer()`) and reports
that frozen value from `snapshot()` for as long as the stage isn't
`awaiting_answer`, rather than a live clock read; the freeze clears
(clock resumes for real) once `ask_question()` fires for the next
question. `app.js` tracks the current stage and only interpolates the
display between polls while `awaiting_answer`, holding steady
otherwise.

Regression tests:
`test_snapshot_freezes_remaining_seconds_once_answer_is_recorded` and
`test_snapshot_resumes_live_ticking_once_next_question_is_asked` in
`test_web_session_ui.py`; the first confirmed to fail against the
pre-fix code (two `snapshot()` calls 0.1s apart returned different
values while frozen, when they should have matched exactly) before
fixing.

**Resumed countdown was lower than where it had been frozen -- a real
core bug, not a web-UI one.** Once the above was fixed, the freeze
itself turned out to be correct but the *value it resumed at* still
wasn't -- reported from real use, noticed on every single question.
Traced to `orchestrator.py`, not this package: `_maybe_queue_followup()`
(which calls `ClassificationProvider.classify()`, a real synchronous LLM
call) was never wrapped in `timer.excluding()` the way
`generate_question()`'s calls already were, so its latency silently
counted against the person's timed session on every answer -- a Phase 7
oversight the CLI never surfaced because `RichSessionUI` doesn't render
a countdown during that gap at all. Full write-up, fix, and regression
test: `docs/system-design/12-phase-7-evaluator-design.md` §12.10 (that's
the bug's actual home; this doc only records that Phase 10 is what
surfaced it).

## 15.14 Post-merge refinements

Small, non-architectural follow-ups made after the initial merge, on
real feedback:

- **Named "viva-web".** The browser interface's page `<title>`/`<h1>`,
  the FastAPI app's own `title=`, and the `viva serve` startup banner
  all say "viva-web" now, distinct from "viva-cli" (the whole project).
  No behavior change. See the naming note at the top of this doc.
- **Favicon.** `static/favicon.svg` (a small terminal-chevron + cursor-
  dot mark, matching `style.css`'s existing dark-panel/accent-blue
  palette) plus a `<link rel="icon">` and an explicit
  `GET /favicon.ico` route -- browsers request `/favicon.ico` directly
  regardless of the `<link>` tag, so both are needed. §15.13 covers this
  as a small user-visible fix (a 404 in the server log on every page
  load), not an architectural bug.
- **`SessionRegistry`'s session-id wait bumped from 10s to 120s**
  (`registry.py`'s `_SESSION_ID_WAIT_SECONDS`). Raised on request,
  against a concern that a slower local model analyzing a larger repo
  could need more time. Worth being precise about what this constant
  actually bounds, since the concern and the mechanism don't quite
  line up: it only waits for `WebSessionUI.session_started()`, which
  `Orchestrator.start()`/`.resume()` call *before* cloning or analysis
  ever begins (§15.3) -- so it was never actually gated by repo size or
  model speed, and resolves in milliseconds regardless. The person's
  actual answer-time budget (`viva_duration_minutes`) is unaffected
  either way: `AnswerTimer` doesn't start until the entire setup
  pipeline (clone/analyze/index/plan) has already finished
  (`orchestrator.py`'s `_run_live_session`, unchanged since Phase 6),
  and question-generation latency between questions is wrapped in
  `timer.excluding()` (FR17) -- both true on the CLI already, unchanged
  by this phase. The bump was made anyway: it's the one genuine timeout
  construct in the whole web layer, costs nothing since it's never
  exercised anywhere near that long, and 120s is a reasonable insurance
  margin against something unrelated going wrong (a slow disk, SQLite
  contention) rather than against slow analysis specifically.
- **"Clear output" button on the cleanup panel.** `POST /api/cleanup`'s
  JSON result was shown in a `<pre>` that, once populated, stayed on
  screen indefinitely with no way to dismiss it short of reloading the
  page. Added a small "Clear output" button next to it
  (`static/index.html`/`app.js`) that hides the result and itself --
  purely a frontend affordance, no API change.
- **Sessions table overflow, and general spacing.** Root cause of the
  overflow: `table` had no `table-layout`/width containment and no
  scroll container of its own, so a long `session_id` or repo URL cell
  could force the table wider than its `.card`, bleeding past the
  page's own layout -- and `style.css` had no `@media` rules at all, so
  nothing adapted on a narrow viewport either. Fixed at the source: the
  sessions table is now wrapped in a `.table-scroll` container
  (`overflow-x: auto`, scoped to just that element) with
  `table-layout: fixed` and per-cell `text-overflow: ellipsis`, the
  `session_id`/repo cells are truncated in `app.js` with the full value
  in a `title` tooltip, `body` gained `overflow-x: hidden` as a backstop,
  and a `@media (max-width: 480px)` block tightens padding/gaps on small
  screens. Also increased spacing throughout (`main`'s gap between
  cards, `.card` internal padding, label margins) on the general
  "spacing needs adjustment" feedback -- a readability/polish pass, not
  a correctness fix, so no regression test; verified by hand-tracing the
  CSS against the markup for both the table-overflow scenario (a long
  session id/URL) and a narrow (480px) viewport.
- **Report view renders real HTML, with explicit download buttons.**
  Overrides the original §15.12 item 3 decision (`<pre>`-as-plaintext,
  zero new frontend dependency) on request. `report.py` gained
  `render_html()`, a third renderer alongside `render_markdown`/
  `render_json`, built from the same structured `Report` object (no
  Markdown-parsing step to get wrong or keep in sync) and used by
  `GET /report?format=html`. Every string in it is `html.escape()`-d
  before embedding, since a `Report`'s content can originate from LLM
  output grounded in the analyzed repo -- a repo crafted to
  prompt-inject HTML/script into an answer must not be able to run
  script in whoever views the report. The report view's old "raw JSON"
  link is now two explicit download buttons
  (`GET /report?format=md|json&download=true`, which sets
  `Content-Disposition: attachment` server-side so the browser saves a
  file instead of navigating to it).
