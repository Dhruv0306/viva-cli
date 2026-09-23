# Plan — AI Project Viva Preparation Tool

Each phase is independently testable and produces a working, demoable slice.

## Phase 0 — Walking Skeleton
- Thinnest possible slice through the *whole* pipeline: ingest one small
  test repo → stub/minimal analysis → 1 hardcoded question → CLI captures
  an answer → 1 real structured evaluation call against the local model →
  bare-bones report.
- Purpose: de-risk the two assumptions most likely to force a redesign
  before real effort is sunk — local-model structured-output reliability
  (design.md §4) and the independent-timer plumbing (design.md §7) —
  rather than discovering problems with them in Phase 6/7.
- **Exit criteria:** one schema-validated evaluation produced by the local
  model end-to-end, and a timer that demonstrably excludes LLM latency from
  the user-facing clock. This must include manual review of the free-text
  critique's groundedness (does it cite real code, not invented claims),
  not just JSON schema conformance — schema-valid-but-hallucinated output
  is a Phase 0 failure, not a pass. If `qwen2.5-coder:7b` can't reliably
  clear this bar, that's the point of finding out now, in Phase 0, not
  after Phase 3–7 are built around it.

## Phase 1 — Scaffold
- Repo structure, `.env` loader, config validation.
- Confirm a basic Ollama call works end-to-end.
- Small `LLM_MODEL` pressure-test (see `docs/system-design/04-open-questions.md`
  item 5): run the same fixed set of 3-5 sample answers, N=4-5 repetitions
  each, against candidate models (at least `qwen2.5-coder:7b` and
  `qwen3.5:latest`), and record classification stability and citation-
  compliance rate per model. Motivated by Phase 0 manual testing, which
  found a within-model classification/rationale inconsistency on
  `qwen2.5-coder:7b` and a between-model gap in citation compliance —
  small enough sample sizes (n=1-2 per model) that this needs to be
  confirmed properly before `LLM_MODEL`'s default is finalized in
  `.env.example`.
- **Exit criteria:** `python -m viva --version`-style smoke test passes;
  config loads and validates required env vars; pressure-test results are
  recorded (doesn't have to change the default model, but the choice must
  be evidence-based, not carried over from Phase 0's placeholder default).

## Phase 2 — Ingestion
- Clone + walk + hard-exclusion filtering (FR2).
- Stack detection (FR5).
- **Exit criteria:** running against 2–3 real test repos of varying size produces a correct filtered file list, including one repo that exceeds the 500-file cap.

## Phase 3 — Analysis
- tree-sitter extraction (FR6).
- Map-reduce Project Profile generation (FR7), including the hierarchical
  reduce fallback (`docs/system-design/06-cli-contract-and-profile-scaling.md`
  §6.2) for repos where per-module summaries themselves overflow the
  reduce-step context.
- **Exit criteria:** manually review Project Profile quality on test repos
  before moving on — everything downstream depends on this. Must include at
  least one test repo with enough modules to force the hierarchical reduce
  path, not only small repos where a single flat reduce suffices.

## Phase 4 — RAG Indexing
- Chunking, embedding, vector store population (FR9–FR11).
- **Exit criteria:** manual retrieval queries return relevant, correctly-scoped chunks.

## Phase 5 — Question Generation
- Category-based, grounded question generation (FR12–FR13).
- **Exit criteria:** generated questions manually reviewed against test repos for grounding accuracy and category coverage.

## Phase 6 — Session Loop
- CLI, timer, state machine, persistence — no evaluation yet.
- Implement `viva start`, `viva resume`, and `viva list` per the CLI
  contract (`docs/system-design/06-cli-contract-and-profile-scaling.md` §6.1).
- **Exit criteria:** a full timed viva runs end-to-end with correct timing
  behavior (FR16–FR20), and `viva list`/`viva resume` behave per the
  contract, including the error case of resuming an already-`COMPLETE`
  session.

## Phase 7 — Evaluation
- Grounded, structured per-question evaluation (FR21–FR24).
- **Exit criteria:** evaluation output validated against schema on all test repos; ungrounded criticisms verified absent.

## Phase 8 — Reporting
- Aggregation and Markdown report generation (FR25–FR27).
- Implement `viva report` per the CLI contract, including both output
  formats and the partial-report error case
  (`docs/system-design/06-cli-contract-and-profile-scaling.md` §6.1).
- **Exit criteria:** report reviewed for usefulness/actionability, not just structural completeness. `viva report --format json` validated against a schema, not just the Markdown path.

## Phase 9 — Polish
- Error handling for bad URLs, model timeouts, huge repos.
- Config validation, resume-session support.
- Repo/index cleanup and retention policy implemented and tested (NFR7) —
  do not leave this implicit; it has no other phase owner.
- ~~Stretch: simple web UI.~~ **Deferred.** Audited out of Phase 9's real
  scope in `docs/system-design/14-phase-9-polish-design.md` §14.1; picked
  up as its own phase below rather than reopening a merged phase.

## Phase 10 — Web UI
- A local FastAPI server (`viva serve`) exposing the same operations as
  the CLI contract (`docs/system-design/06-cli-contract-and-profile-
  scaling.md` §6.1) — start, resume, list, report, cleanup — plus the
  live question/answer loop, fronted by a single static HTML+JS page
  (no frontend framework/build step).
- Design doc: `docs/system-design/15-phase-10-web-ui-design.md`. Key
  decision: a queue-backed `WebSessionUI` (new `SessionUI` implementation)
  bridges the Orchestrator's blocking `read_answer()` call to HTTP
  request/response by running each live session's `Orchestrator.start`/
  `.resume` call on a background thread — no change to `Orchestrator`
  or the `SessionUI` interface itself.
- **Exit criteria:** a full timed viva run end-to-end through the browser
  (start → live Q&A → report), plus `viva list`/`viva resume`/`viva
  cleanup` all reachable from the UI, validated against a real local
  Ollama instance — not just `TestClient`-mocked coverage.

## Phase 11 — Voice I/O (CLI)
- Speak questions aloud (Piper) and answer by talking instead of typing
  (faster-whisper), entirely local, CLI only. Opt-in via
  `VOICE_ENABLED=true`; `viva voice setup` pre-pulls both models so a
  live session never stalls on a first-run download.
- Design doc: `docs/system-design/16-phase-11-voice-io-design.md`. Key
  decision: recording time counts as answering time (same as typing),
  only STT/TTS *compute* is excluded from the answer clock
  (`timer.excluding()`) — the new `VoiceIO` component's `record()`/
  `transcribe()` split is what makes that distinction possible at the
  call site.
- **Exit criteria:** a full timed voice-mode viva run validated against
  real microphone/speaker hardware, not just mocked unit coverage —
  several real-world bugs (an incompatible-GPU crash, a missing
  countdown, transcription accuracy) were only found this way and are
  logged in the design doc's §16.9.

## Phase 12 — Web Voice I/O
- Brings Phase 11's voice mode to `viva serve` ("viva room"): spoken
  questions and spoken answers in the browser, using the same
  `LocalVoiceIO` engine. Per-session toggle — each browser tab opts in
  independently, with no new session state on the backend.
- Design doc: `docs/system-design/17-phase-12-web-voice-io-design.md`.
  Key decisions: server-side Piper synthesis served as WAV (not
  browser-native `speechSynthesis`, for voice-quality consistency with
  the CLI) and raw-PCM capture via `AudioWorklet` (not `MediaRecorder`,
  to avoid a new server-side audio-decode dependency).
- **Exit criteria:** a full timed voice-mode viva run through a real
  browser end-to-end (spoken question → recorded answer → transcribed
  → graded), validated against a real microphone, not just
  `TestClient`-mocked endpoint coverage.

## Phase 13 — Architecture-Tier Questions
- Splits the single `architecture` category into an extensible set of
  topics (system overview, pipeline/data flow, security boundaries,
  external integrations, concurrency), each capable of holding more than
  one question, asked ahead of the other four categories rather than
  interleaved with them from question one.
- Design doc: `docs/system-design/18-phase-13-architecture-tier-
  questions-design.md`. Key decisions: ordering is enforced by a
  `phase` key in the session loop's ranking function (not by plan
  insertion order, which doesn't survive the existing category-breadth
  tie-break — see design doc §18.6), a separate architecture-tier system
  prompt permits component/module-level specificity instead of the
  implementation tier's exact-function requirement, and `max_questions`
  is derived from `VIVA_DURATION_MINUTES` with live-loop replenishment on
  exhaustion rather than ending the session early.
- **Exit criteria:** a real-repo timed session where every architecture-
  topic question is asked before any implementation-tier question
  starts, a fast-answering session that exhausts the initial plan still
  runs until the timer expires (via replenishment) rather than hitting
  `QUESTIONS_EXHAUSTED` early, and a thin repo with no groundable
  security/concurrency code skips those topics without error.

## Phase 14 — Input Validation & Quick Security Fixes
- Closes the fast, low-complexity items from the September 2026 panel
  review (`docs/system-design/19-panel-review-findings-2026-09.md`),
  bundled into one phase because each fix is small, independently
  testable, and doesn't require a design decision to be made first, only
  a regression test written against pre-fix code, per the usual pattern.
- Design doc: `docs/system-design/20-phase-14-security-hardening-
  design.md`. Key decision: §19.4.2, §19.4.3, and §19.5.2 turned out to
  be one fix, not three — all three trace back to `ingest/clone.py`'s
  `_repo_slug()`, replaced by a single `validate_repo_url()` that both
  `clone_repo()` and `Orchestrator.start()` call, rather than three
  independently-maintained checks. §19.3.1 gets a new
  `InvalidParametersError(OrchestratorError)` subtype, following the
  same pattern as the three `OrchestratorError` subtypes that already
  exist, so both `cli.py` and `web/app.py` can map it to the right
  exit code / status code ahead of their generic catch-alls. §19.5.1 is
  a one-file `innerHTML` → `textContent` fix with no Python-side
  dependency.
- **§19.4.3 — validate `repo_url` scheme before it reaches `git clone`.**
  Parse with `urlsplit` and reject anything outside an explicit
  `{https, ssh}` allowlist before `clone.py` ever builds a clone URL.
  Highest priority item in the whole review — the only one with a
  plausible path to code execution rather than disclosure or corruption.
- **§19.4.2 — fix the `GITHUB_TOKEN` host-bypass.** Validate the *parsed*
  host from the same `urlsplit` call used for §19.4.3 (not a tail-anchored
  regex against the raw string) before `_with_token` attaches the token.
  One validation function change covers both this and §19.4.3.
- **§19.5.1 — stop building the session-list table row with `innerHTML`.**
  Switch `web/static/app.js`'s row construction to `textContent`
  assignments, matching every other dynamic value in the same file.
- **§19.5.2 — reject malformed `repo_url` at the API boundary** (empty,
  absurdly long, no parseable scheme/netloc) before a session row is
  persisted, so garbage values don't reach the store in the first place.
- **§19.3.1 — fix the `duration_minutes` falsy-zero bug.** Replace the
  `duration_minutes or self.config.viva_duration_minutes` fallback in
  `orchestrator.py` with an explicit `is not None` check, and reject
  non-positive values via the new `InvalidParametersError` rather than a
  Pydantic `Field` constraint (which would return `422`, not the `400`
  this API's own documented contract specifies).
- **Exit criteria:** a fixture URL of the exact
  `https://attacker.example/x/github.com/owner/repo` shape (§19.4.2) is
  confirmed to never receive the token, both a non-`{https,ssh}`-scheme
  `repo_url` and a `duration_minutes` of `0` and `-1` are rejected with a
  clear error before any session row is written, and the session-list page
  renders a `repo_url` containing `<img src=x onerror=...>` as inert text
  in the browser, not as executed markup. Each of the five items above
  gets its own regression test confirmed to fail against pre-fix code.

## Phase 15 — `viva serve` Authentication
- Addresses §19.4.1: every route in `web/app.py` is currently
  unauthenticated, and `--host 0.0.0.0` is a normal, one-flag-away choice
  for anyone demoing the tool on a shared network.
- Unlike Phase 14, this needs a design decision confirmed before
  implementation, per the usual "agree before code" step — candidates
  worth weighing: a loud warning gate on any non-loopback `--host` value
  (cheapest, lowest friction, no real access control); a shared-secret
  token printed at startup and required on every route once bound
  non-locally; or a fuller session-cookie/login flow, which is likely
  disproportionate for a local-first single-user tool and should be
  argued against explicitly in the design doc rather than silently
  skipped.
- Design doc: `docs/system-design/21-phase-15-serve-authentication-
  design.md`. Recommends a shared-secret bearer token, generated fresh
  per `viva serve` invocation, enforced on `/api/*` only and only when
  the bind address isn't loopback — the default, frictionless case is
  untouched. Two alternatives (warning-only flag; full login/session
  system) are written up and rejected in the doc with reasoning. This
  recommendation still needs to be confirmed before implementation
  starts, per the "agree before code" step this phase was split out
  for in the first place.
- **Exit criteria:** `viva serve --host 0.0.0.0` either refuses to start
  without an explicit second acknowledgment flag, or requires a
  credential on every route once bound non-locally — the chosen behavior
  is validated against a real non-loopback bind, not just a unit test of
  the check itself.

## Phase 16 — Grading Integrity & Retrieval Observability
- Groups the remaining medium/low items that affect question and
  evaluation quality rather than infrastructure security, from
  `docs/system-design/19-panel-review-findings-2026-09.md`. Design doc:
  `docs/system-design/22-phase-16-grading-integrity-observability-
  design.md`. Two of the three findings turned out to be one mechanism
  (§19.1.2's thin-retrieval detection and §19.6.2's logging both read
  the same `distance` value `VectorStore.query()` already returns), and
  the "redistribute the question budget" behavior §19.1.2 asked for
  already exists in `orchestrator.py`'s live loop (a `None`-returning
  `generate_question()` is already marked `SKIPPED_NO_GROUNDING` and the
  loop already moves to the next ranked candidate) — it just wasn't
  under test, which this phase also fixes.
- **§19.1.2 + §19.6.2 — retrieval-quality threshold and logging,
  shipped as one change in two patches.** Patch A adds the distance-
  based filter to `retrieve_grounding_chunks()` and the log line, but
  ships with filtering *disabled by default*
  (`Config.max_retrieval_distance: float | None = None`) — there's no
  textbook-correct L2 distance threshold for this project's embeddings
  without real data, the same reasoning that led to pressure-testing
  before picking `gemma4:e4b` rather than guessing. Patch B sets a real
  default once patch A's logging has produced actual numbers from a
  real session against a real repo.
- **§19.1.1 — instruction-injection boundary for retrieved repo
  content.** Not a new delimiter scheme — the existing `[CODE_CONTEXT]`/
  `[GROUND_TRUTH_CODE_CONTEXT]` labeled-section convention already
  provides the structural boundary. One paragraph added to each of the
  four system prompts in `llm_client.py` stating that content inside
  those sections (and `[USER_ANSWER]`) is data to reason about, never
  instructions to follow. A golden-repo fixture with a deliberately
  adversarial docstring is added for manual pressure-testing against
  the real configured model during this phase's real-world validation
  step — whether a model actually resists the injection is a model-
  behavior question CI can't assert, only prompt-content presence can
  be checked automatically.
- **Exit criteria:** patch A's logging shows real distance numbers from
  at least one real session, informing patch B's threshold ✅ — 4
  sessions across 3 repos and 6 categories, resolved to
  `MAX_RETRIEVAL_DISTANCE=0.85` (docs/system-design/22-phase-16-
  grading-integrity-observability-design.md §22.2.2.1); a thin/
  sparse test repo triggers at least one topic skip with a
  corresponding log line, proven by a new orchestrator-level test (none
  existed before this phase) rather than asserted as already covered
  ✅ — confirmed live with `MAX_RETRIEVAL_DISTANCE=0.01` against
  `octocat/Hello-World` (17/17 questions skipped, session still
  completed cleanly); the adversarial-docstring fixture is manually
  confirmed, against the real configured model, not to flip the
  evaluator's classification ✅ — `not_attempted`, not `correct`, for a
  deliberately wrong answer; and Phase 13's existing exit-criteria
  repos are re-run to confirm no regression in question grounding
  quality ✅ — `viva-cli` itself, 7/7 questions asked, no duplicates
  after the `_maybe_queue_followup` fix below.
- **Bonus finding, fixed in the same phase:** real-world testing of the
  above surfaced a separate, unrelated bug — `_maybe_queue_followup()`
  in `orchestrator.py` silently dropped `architecture_topic` when
  constructing a follow-up item, violating `QuestionPlanItem`'s own
  documented invariant and collapsing every architecture follow-up
  into one generic, topic-less retrieval bucket regardless of which of
  the five real topics its parent was about. Produced literal duplicate
  questions in a live session (5 of 14 questions asked were word-for-
  word identical). Fixed and covered by a new regression test.

## Phase 17 — CLI Logging Hygiene
- Root cause: Phase 13's `logging.basicConfig(level=logging.INFO, ...)`
  in `cli.py`'s `main()` callback configures the *root* logger, and
  `httpx` (used for every Ollama call) logs each request at INFO and
  propagates to the root logger by default, same as viva's own loggers.
  The result, confirmed during Phase 14's real-world verification run:
  a `viva start` session's terminal fills with
  `httpx: HTTP Request: POST http://localhost:11434/api/... "HTTP/1.1
  200 OK"` lines interleaved with the actual question/answer UI, one per
  Ollama call (chat, embed, occasionally show) — noisy enough during a
  live multi-question session to bury the planning-decision log line
  Phase 13 specifically added for diagnosability.
- **Not a removal.** The Ollama request trace has real debugging value
  (confirming retries, spotting a hung request, correlating timing with
  a slow answer) — silently dropping it trades one diagnosability gap
  for another, the same mistake Phase 13's design doc already called out
  for the planning-decision log. It moves to a file instead of stdout.
- **Design:**
  - A dedicated `logs/` directory (created if missing, alongside the
    existing `session_db_path` convention rather than requiring its own
    config field).
  - One file per day, named `log_<YYYY_MM_DD>.log` (e.g.
    `log_2026_09_15.log`), so a day's worth of Ollama request traces
    stay together and the file naming makes retention trivial to reason
    about.
  - Only `httpx` (and `httpcore`, which `httpx` logs through for
    lower-level connection events) get redirected to the file handler
    with `propagate=False`, so they stop reaching the root logger's
    console handler entirely. Viva's own loggers (`orchestrator`,
    `evaluator`, `llm_client`, `analyzer.extract`) keep logging to
    console exactly as Phase 13 set up — this phase narrows *what* goes
    to the terminal, it doesn't touch the existing on-screen
    diagnosability work.
  - **3-day retention**, implemented as a housekeeping step (delete any
    `logs/log_*.log` file more than 3 days old) run once at CLI startup
    in the same `main()` callback that configures logging — the same
    "run housekeeping on startup" shape `cleanup.py`'s `run_cleanup()`
    already established for stale sessions, just scoped to log files
    instead of session rows. No new CLI command needed; this isn't
    something a user needs to trigger on demand the way session cleanup
    is.
  - Only configured in `cli.py`'s `main()`, matching the existing
    Phase 13 comment's reasoning for why logging setup lives there and
    not in `web/app.py`'s `create_app()` (so importing the FastAPI app
    for tests doesn't also start writing log files or touch the
    filesystem).
- **Exit criteria:** a `viva start` session against a real Ollama
  instance produces a clean terminal, no `httpx`/`httpcore` lines mixed
  into the question/answer UI, while a same-day `logs/log_<today>.log`
  file exists and contains those request traces. Running `viva start`
  again the next day produces a second, separate dated file rather than
  appending to the first. A log file dated more than 3 days ago is gone
  after the next CLI invocation; one dated within the last 3 days is
  untouched. Existing Phase 13 exit criteria (planning-decision log line
  visible on the console) still hold — this phase must not silence that
  the way it silences `httpx`.
- **Verified**, live, on 2026-09-18: terminal stayed clean across
  multiple real sessions (no `httpx`/`httpcore` lines, planning-decision
  line still visible); `logs/log_2026_09_17.log` and
  `logs/log_2026_09_18.log` both exist as separate per-day files, the
  latter's tail full of the `HTTP Request: POST http://localhost:11434
  /...` lines that used to be on-screen; a simulated 10-day-old log file
  was removed by the next `viva` invocation while a simulated 1-day-old
  one was kept. All five exit criteria met.
- **Extended, 2026-09-19:** `viva.questiongen.retrieval`'s own per-
  question INFO line (Phase 16 §19.6.2) joined `httpx`/`httpcore` on
  the redirected-to-file list, once real usage showed it was just as
  disruptive to the live question/answer UI, printed once per question
  asked. `orchestrator`'s planning-decision line (fires once per
  session, not once per question) stays on console, unchanged.

## Phase 18 — Dependency & Auth Hygiene
- Design doc: `docs/system-design/23-phase-18-dependency-auth-hygiene-
  design.md`.
- Root cause, three unrelated small items bundled the way Phase 14
  bundled multiple unrelated panel-review fixes into one phase:
  1. `pyproject.toml`'s `dev` extra lists `httpx2>=2.0,<3.0`, not
     `httpx`. `httpx2` is a real, unrelated PyPI package, not an alias.
     `fastapi.testclient.TestClient` needs the real `httpx` at runtime;
     this only works today because `ollama` (a direct dependency)
     transitively pulls in real `httpx>=0.27`, masking the typo. Found
     while scoping this phase: `requirements.txt` is separately missing
     `fastapi`/`uvicorn` entirely, so the README's own documented
     `pip install -r requirements.txt` path leaves `viva serve`
     unrunnable until this phase's fix.
  2. `web/app.py`'s `_require_token` middleware compares the supplied
     token with plain `supplied != token` instead of
     `hmac.compare_digest`. Not a response to a demonstrated exploit —
     the token is a 192-bit `secrets.token_urlsafe(24)` value, a timing
     attack is impractical — but constant-time comparison is the
     correct primitive for any credential check, on principle.
  3. `pyproject.toml` has `license = { text = "TBD" }` and the README's
     License section just says "TBD." Public repo, `CONTRIBUTING.md`
     actively invites contributions, but with no LICENSE file default
     copyright applies and nobody has a clear right to use, fork, or
     redistribute the code.
- **Design:**
  - Item 1: `httpx2>=2.0,<3.0` → `httpx>=0.27,<1.0` in
    `pyproject.toml`'s `dev` extra, matching the version floor `ollama`
    itself already requires; `requirements.txt` gets `fastapi`/
    `uvicorn` added to its base section (same ranges as
    `pyproject.toml`) and `httpx` added to its `# dev/test` section, so
    both manifests actually provide what they claim to.
  - Item 2: swap the equality check for `hmac.compare_digest(supplied
    or "", token)` — guard the `None` case explicitly, since
    `compare_digest` requires two strings (or two bytes objects) of
    matching type, and `supplied` is `None` when no token/header was
    sent at all.
  - Item 3: add an MIT `LICENSE` file at repo root (fits "no API keys,
    nothing leaves your machine, freely usable"); update
    `pyproject.toml`'s `license` field to `{ text = "MIT" }`; replace
    the README's "TBD" with a one-line MIT summary linking to the file.
- **Exit criteria:**
  - A clean `pip install -e ".[dev]"` in a fresh venv shows `httpx`
    (not `httpx2`) in `pip list`; `pytest -q` still passes.
  - A separate clean `pip install -r requirements.txt` (the README's
    documented path) succeeds at `import fastapi, uvicorn` and
    `viva serve --help` with no import error.
  - A test asserting `_require_token` still rejects a wrong token and
    still accepts the correct one (behavior-preserving, so this is a
    check that the swap didn't regress anything, not a pre-fix-failing
    regression test — no user-observable behavior changes).
  - `viva serve --host 0.0.0.0` real-world run: correct token still
    accepted, wrong token still 401s, missing token still 401s.
  - `LICENSE` file present at repo root; `pyproject.toml` and README no
    longer say "TBD"; `pip show viva-cli` after a clean install reports
    `License: MIT`.
  - `CHANGELOG.md`'s `[Unreleased]` section and this Phase 18 entry's
    own `**Verified**` line are both written once the above are
    actually confirmed, not before.
- **Corrected, 2026-09-20:** item 1's original framing above was wrong.
  `httpx2` is not a typo for `httpx` — it's Pydantic's actively
  maintained fork, now Starlette's preferred `TestClient` dependency
  (`httpx` itself has had no release since 2024). `pyproject.toml`'s
  original `httpx2>=2.0,<3.0` line was correct as written and needed no
  change. The only real gap was that `requirements.txt`'s dev/test
  section never had `httpx2` (or `httpx`) in it at all — it gets
  `httpx2>=2.0,<3.0` added, matching `pyproject.toml`'s existing
  choice, not a switch to `httpx`. The `fastapi`/`uvicorn` additions to
  `requirements.txt`'s base section are unaffected. Full root-cause
  analysis: design doc §23.9.
- **Verified**, 2026-09-21: all three patches landed and confirmed —
  fresh `pip install -e ".[dev]"` and `pip install -r requirements.txt`
  both succeed with the right packages present; `_require_token`'s
  `hmac.compare_digest` swap confirmed behavior-preserving against
  `test_web_app.py`'s existing missing/wrong/correct-token tests (no new
  tests needed) plus a real `mypy --strict` catch along the way (a new
  `type-var` error on the `compare_digest` call, fixed with
  `assert token is not None` to narrow the type, since `require_token`
  and `token` are set together at `create_app()` time and never
  reassigned); fresh `pip install .` reports `License: MIT` via
  `pip show`. Full test suite (641 tests), `ruff`, and `mypy` all clean
  at every commit.

## Phase 19 — CI Quality Gates
- Design doc: `docs/system-design/24-phase-19-ci-quality-gates-design.md`.
- Root cause: no static analysis runs in CI today (checked
  `pyproject.toml`, `CONTRIBUTING.md`, `.github/workflows/tests.yml` —
  none reference `ruff`, `mypy`, `black`, or `pytest-cov`). The
  Phase 18 `httpx2` typo is exactly the class of mistake a linter
  catches for free before merge, not after a deep-dive review finds it.
- **Design:**
  - `ruff`: default rule set plus `B` (bugbear) and `UP` (pyupgrade),
    run via a new CI job and documented in `CONTRIBUTING.md`.
  - `mypy`: start at `--strict` against `src/viva/` — the codebase is
    already fully typed (dataclasses, type hints throughout), so this
    is a "see what a strict pass finds" run rather than a ratchet-up
    from a loose baseline; scope narrows in the design doc if `--strict`
    turns up more noise than signal on first run.
  - `pytest-cov`: added for visibility first (a coverage report in CI
    output), not a hard minimum threshold — decide in the design doc
    whether a threshold gets added later once a baseline number exists.
  - New dependencies (`ruff`, `mypy`, `pytest-cov`) land in the `dev`
    extra, which by this phase already has the Phase 18 `httpx` fix in
    it.
- **Sequencing note:** land this **before or alongside** Phase 18, not
  after — Phase 18 is exactly the kind of change these gates should be
  catching, and demonstrating that on a real PR (see exit criteria) only
  works if the gates exist first.
- **Exit criteria:** clean `ruff`/`mypy`/`pytest-cov` runs against
  current `main`; a throwaway branch that reintroduces the `httpx2`-style
  typo confirmed to fail the new `ruff` check (demonstrating the gate
  actually catches this class of error, not just a hypothetical); a real
  PR that trips one `ruff` rule and one `mypy` error confirmed to block
  CI the same way a `pytest` failure already does.
- **Corrected, 2026-09-20:** two things above turned out wrong once
  actually measured (design doc §24.1-§24.2, run directly against this
  codebase rather than estimated). First, the dependency bullet: the
  three new tools land in `requirements.txt` too, not just
  `pyproject.toml`'s `dev` extra — Phase 18's own correction (§23.9) is
  exactly the lesson this needed, and this doc almost repeated the
  mistake by only mentioning `pyproject.toml`. Second, the exit
  criteria's `httpx2`-typo claim: `ruff`/`mypy` check Python source, not
  `pyproject.toml`'s dependency list, so neither tool would ever have
  caught that specific mistake — confirmed by checking rather than
  assumed, since asserting a gate "would have caught" something it was
  never designed to check is its own version of the same error Phase 18
  made. Corrected exit criteria: see design doc §24.6, including the
  actually-relevant checks (`--cov-fail-under=90` regression test,
  §24.1.3/§24.2.3's own fixes and per-module overrides landing clean).
- **Sequencing note, sequel:** by the time this design doc was written,
  Phase 18's patch 1 (dependency manifests) had already landed —
  "before Phase 18" from the original note above didn't happen, and
  that's fine; the note's actual point (these gates should exist before
  more hand-written fixes land) still holds looking forward to Phase
  18's remaining patches 2-3.
- **Verified**, 2026-09-21: `ruff check .` and `mypy src/viva` both
  clean against real runs, not assumed; full test suite (641 tests)
  passes; `pytest --cov-fail-under=90` passes at 95.19% measured
  coverage. All 63 real (non-`E501`) `ruff` findings from §24.1
  resolved by hand or `--fix`, each individually judged rather than
  bulk-applied — see `CHANGELOG.md`'s `[Unreleased]` entry for the
  substantive ones (`from None`, `strict=True`, the two unused-variable
  removals). One thing caught only while actually running `ruff check .`
  against the whole repo, not anticipated in the design doc:
  `tests/fixtures/golden_repos/` (synthetic ingest test data, not this
  project's own code) needed excluding, same reasoning as
  `[tool.pytest.ini_options].norecursedirs` already applies to pytest —
  added to `[tool.ruff]`'s `exclude` before any fix touched it.

## Phase 20 — Serve Hardening & Onboarding
- Design doc: `docs/system-design/25-phase-20-serve-hardening-
  onboarding-design.md`.
- Root cause, two items both touching the `viva serve` / first-run path:
  1. No rate limiting on `POST /api/sessions`. Once a non-loopback bind
     is in use (Phase 15), any request carrying the correct token can
     start unlimited concurrent sessions, each triggering a real `git
     clone` plus Ollama load — a local resource-exhaustion vector for
     anyone who has the token, distinct from the "no token at all" gap
     Phase 15 already closed.
  2. Getting a new install running today requires three separate manual
     steps (start Ollama, `ollama pull` two specific models, `cp
     .env.example .env`) with no single command to verify all three
     succeeded before `viva start` is attempted — the given care that's
     gone into diagnosability elsewhere (Phase 13/16/17's logging work)
     hasn't yet extended to first-run setup itself.
- **Design:**
  - Rate limiting: a per-token in-memory counter on `SessionRegistry`
    capping concurrent `IN_PROGRESS` sessions started by the same
    token (small fixed limit, e.g. 3) — matches the "simplest thing
    that works" reasoning already used for the token design itself
    (`21-phase-15-serve-authentication-design.md`), not a general
    request-rate middleware or an external library. Returns 429 with a
    clear message when the cap is hit. Loopback (no token) case is
    unaffected — the cap only applies when `require_token` is true.
  - `viva doctor` (new CLI command): checks Ollama reachability against
    `Config.ollama_host`, confirms `LLM_MODEL` and `EMBEDDING_MODEL` are
    both pulled (via the Ollama API's model-list endpoint, not a shell
    call to `ollama list`), prints a pass/fail line per check with a
    concrete fix instruction (e.g. the exact `ollama pull <model>` to
    run), exits 0 if all checks pass and 1 otherwise. No changes to
    `viva start`/`serve` themselves — this is a new, separate,
    read-only diagnostic command.
- **Exit criteria:**
  - Rate limit: a test asserting the (cap + 1)th concurrent session
    request from the same token is rejected with 429, confirmed failing
    against pre-fix code first; real-world run against `viva serve
    --host 0.0.0.0` confirms the same behavior live.
  - `viva doctor`: unit tests mocking Ollama reachable/unreachable and
    model-present/absent, asserting the correct exit code and message
    per case; real-world run with Ollama stopped, then with a model
    missing, then fully correct, confirming each state reports
    accurately.
- **Corrected, 2026-09-21:** the rate-limiting design above turned out
  wrong in two places once checked directly against `web/registry.py`
  rather than trusting this bullet's own wording. "Per-token" isn't a
  real dimension — `create_app()` generates exactly one shared token per
  `viva serve` process, so there's no per-caller identity to key a
  per-token counter on; the cap is process-wide. And
  `SessionRegistry._sessions` never removes a completed session's entry
  (confirmed: no `del`/`.pop()` anywhere against it), so a raw
  `len(self._sessions)` cap would permanently lock the server after a
  handful of *finished* sessions, not actually limit concurrency — the
  cap has to filter on `LiveSession.thread.is_alive()` instead. Also:
  the cap now applies to `resume_session()` too, not just
  `start_session()` (both spawn a live thread and are the same resource
  concern), and the suggested default is 5, not the original "e.g. 3"
  (a solo user with a couple of browser tabs open is a real case this
  shouldn't false-positive on). Full reasoning: design doc §25.1/§25.2.
  Corrected exit criteria: see design doc §25.5, including a test that
  actually keeps a session's thread alive to distinguish "concurrent"
  from "ever started" — the bug this correction exists to avoid.
- **Verified**, 2026-09-21: both halves implemented and confirmed. Rate
  limiting — `test_web_registry.py` gained a test proving
  `active_session_count()` differs from `len(self._sessions)` (2 dict
  entries, 1 actually active — the exact bug §25.1 avoids), plus tests
  for both `start_session()` and `resume_session()` rejecting past the
  cap and recovering once a session frees up; `test_web_app.py` covers
  the HTTP-layer 429 mapping for both endpoints. `viva doctor` — real
  end-to-end runs, not just mocked: a live subprocess call against a
  genuinely unreachable host confirmed the exact output and exit code;
  `viva --help` confirmed the command registers correctly. Tests cover
  all three checks plus, specifically, the §25.3 finding — a mocked
  `ConnectionError` case and a *separate* mocked non-`ConnectionError`
  exception case, proving the bare-`Exception` catch (not a narrower
  one) is what actually closes that gap. One real thing caught only by
  running the command for real: a long single-line "not pulled" message
  word-wrapped mid-command in Rich's console output, split into two
  shorter prints, same fix `cleanup`'s own code already uses for the
  same reason. Full test suite (651 tests), `ruff`, and `mypy` all
  clean at every commit.
- **Real-world bug found during testing, 2026-09-22:** real Windows run
  (a dev checkout with a real `.env`, exactly the setup this phase helps
  with) failed `test_doctor_reports_config_error_and_exits_2` --
  `load_dotenv()` refilled `LLM_MODEL` from disk after
  `monkeypatch.delenv` unset it, so the test exercised the wrong code
  path entirely (exit 1, Ollama-unreachable, instead of exit 2,
  config-error). Known, previously-fixed gotcha in this exact suite
  (`test_cli_cleanup.py`/`test_cli_session.py` both already guard
  against it), missed when writing this phase's own test. Fixed with the
  same `mocker.patch("viva.config.load_dotenv")` pattern already
  established elsewhere; reproduced and confirmed for real, not just
  assumed, by creating an actual `.env` file locally and confirming the
  pre-fix test failed identically before confirming the fix passes.
  Full root-cause analysis: design doc §25.9.

## Phase 21 — Containerized Setup
- Design doc: `docs/system-design/26-phase-21-containerized-setup-
  design.md`.
- Root cause: no `Dockerfile` or `docker-compose.yml` exists. Given the
  whole pitch is "local-first, works against your own Ollama instance,"
  a one-command containerized path removes the biggest onboarding
  friction point for anyone evaluating the tool before committing to a
  full native install.
- **Open question to resolve in the design doc before implementation:**
  does the container bundle Ollama itself (heavier image, works
  standalone) or expect an external Ollama reachable via
  `Config.ollama_host` (lighter image, matches how most people already
  run Ollama on bare metal for GPU access)? Leaning toward the latter —
  voice mode already treats native deps as optional-by-design, and GPU
  passthrough into a container for Ollama is a separate concern this
  phase shouldn't need to own.
- **Design:** `Dockerfile` for `viva serve`; `docker-compose.yml` with a
  volume for `data/` (so sessions persist across container restarts,
  matching `SESSION_DB_PATH`/`VECTOR_DB_PATH`'s existing convention) and
  an `.env` mount; README section documenting the containerized path
  alongside the existing native-install instructions, not replacing
  them.
- **Exit criteria:** `docker compose up` produces a working `viva serve`
  reachable from the host, against a locally-running (host or separately
  containerized, per the open question above) Ollama instance; a session
  started, answered, and reported on entirely through the containerized
  path; restarting the container preserves prior sessions via the
  mounted volume.
- **Decided, 2026-09-22:** external Ollama, not bundled — the open
  question above is resolved, not just leaned on. No GPU passthrough for
  this project's own container to own; the container only ever makes
  HTTP calls to whatever `OLLAMA_HOST` points at. Two things the
  original design bullets above didn't anticipate, found by checking the
  actual codebase rather than reasoning abstractly about "a Dockerfile":
  `Config.ollama_host`'s `http://localhost:11434` default doesn't
  resolve inside a container at all (needs per-platform handling, design
  doc §26.2), and `viva serve`'s own `--host 127.0.0.1` default isn't
  reachable via Docker's port mapping either (§26.3) — though that one
  turns out to interact cleanly with Phase 15's existing auth logic
  rather than needing new code: binding `0.0.0.0` already and
  automatically requires a token. Also not in the original bullets:
  `git` isn't in a slim Python base image by default (§26.4), and
  `tree-sitter-language-pack`'s grammar cache needs its own volume mount,
  separate from `data/` (§26.4). Full design: doc §26.1-§26.7. **This
  design doc could not verify the Dockerfile/compose files themselves
  end-to-end — no Docker in the authoring environment — so real-world
  validation (§26.9) carries more weight than usual before this phase's
  own `**Verified**` line gets written.**
- **Implemented, 2026-09-23, not yet verified on real Docker:**
  `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `entrypoint.sh`,
  and the README's Docker section are all written. Two things found only
  while actually implementing, neither visible in §26.6/§26.7's drafts —
  design doc §26.11 has the full account: the non-root `viva` user from
  §26.5 would have crashed the container on startup (`cli.py`'s
  unconditional `logs_dir.mkdir()` plus a root-owned `data/` bind mount
  → `PermissionError` before the server ever binds), fixed with the
  standard root-then-drop-privileges entrypoint pattern; and `gosu`
  (§26.5/§26.7's original assumption) got swapped for plain `su`, since
  `gosu`'s `apt-get` availability couldn't actually be checked without
  real access to Debian's package repos. **This phase's own
  `**Verified**` line still isn't written — per §26.9, that needs real
  `docker compose up` on real Docker, which hasn't happened yet.**
- **Real-world bug found during testing, 2026-09-23:** real
  `docker compose up --build` on real Windows hardware failed —
  `exec /entrypoint.sh: no such file or directory` on container start,
  despite the build itself completing and the file being right there.
  Classic Windows Git CRLF conversion: no `.gitattributes` existed, so
  `core.autocrlf=true` (Git for Windows' common default) rewrote
  `entrypoint.sh`'s line endings to CRLF on checkout, giving it a
  `#!/bin/sh\r` shebang — the kernel looks for a literal `/bin/sh\r`
  interpreter that doesn't exist, and reports that as "no such file or
  directory," easy to misread as a missing-file problem when it's a
  line-ending one. Fixed two ways, not one: `.gitattributes`
  (`eol=lf` for `*.sh`/`Dockerfile`) so this can't happen on a fresh
  clone, plus a defensive `sed -i 's/\r$//' /entrypoint.sh` added to
  the `Dockerfile` itself so the build is robust even against an
  existing checkout `.gitattributes` can't retroactively fix — the
  second layer means nothing further needs doing on the Windows machine
  that hit this beyond pulling the fix and rebuilding. Full account:
  design doc §26.12.

## Backlog (not yet scheduled)
- **Phase 16 follow-up — validate `MAX_RETRIEVAL_DISTANCE=0.85` against
  the categories/cases no data exists for yet.** The default set in
  Patch B (docs/system-design/22-phase-16-grading-integrity-
  observability-design.md §22.2.2.1) is real, data-backed — not a
  guess — but the data has two known gaps: no `error_handling` or
  `testing_strategy` samples were ever collected, and only one
  thin-repo case (`octocat/Hello-World`) exists. Not blocking, the
  value is already better than the previous "disabled" state either
  way, but worth tracking rather than trusting silently.
  - **Concrete signal to watch for**, from the `Retrieval for
    category=...` log line every real session already prints: an
    `error_handling` or `testing_strategy` question getting
    `SKIPPED_NO_GROUNDING` unexpectedly often on repos that clearly
    have real error-handling code or a real test suite, or the reverse
    — a visibly thin/sparse repo's questions in those two categories
    still coming through as detailed and well-grounded, which would
    suggest 0.85 is too loose for them specifically.
  - **What to do if it happens:** paste the relevant `Retrieval for
    category=error_handling|testing_strategy ...` log lines the same
    way the original data collection worked, distance numbers from a
    few more real sessions are enough to tell whether 0.85 needs a
    per-category value or just a different single number.
  - Revisit the *number*, not the mechanism, if this comes up — the
    filter/logging/skip-and-redistribute machinery itself is already
    proven correct (§22.2.4's test, plus the live
    `MAX_RETRIEVAL_DISTANCE=0.01` forced-skip run).

- **§19.2.2 — split `orchestrator.py`'s planning/ranking logic into its
  own module.** A maintainability refactor, not a behavior change; no
  user-facing exit criteria to attach it to. Deferred the same way the
  Phase 9 web-UI stretch goal was deferred into its own phase rather than
  forced into an unrelated one — revisit once Phase 16 or a future phase
  needs to touch planning/ranking again, since that's the natural trigger
  to do the split rather than as a standalone phase with no functional
  payoff.

## Cross-Cutting: Testing
- A small fixture set of real "golden repos" (a few small, varied-language
  repos) is checked into the test suite from Phase 2 onward and reused
  across Phases 2–8, so profile/question/evaluation quality regressions are
  visible in CI rather than only caught by manual eyeballing.
- Each phase's exit criteria should be re-run against the full golden-repo
  set, not just the repo used during that phase's development.
