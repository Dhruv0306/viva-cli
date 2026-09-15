# System Design Reference

This folder is the detailed reference record behind `../design.md` (the
canonical, build-facing design). Read `../design.md` first if you're
building; come here when you need the reasoning behind a decision, or the
history of what was tried and rejected along the way.

- **[01-resolved-decisions.md](01-resolved-decisions.md)** — the four
  design decisions with the most alternatives considered: file-sampling
  strategy, structured-output reliability approach, and ground-truth
  grounding for evaluation, including the post-validator refinements to
  each.
- **[02-iteration-log.md](02-iteration-log.md)** — the four architecture
  iterations (naive → state machine → component boundaries → post-review
  fixes), each with the specific problems found that forced the next
  revision. Read this to understand *why* the design has the shape it does,
  not just what the shape is.
- **[03-final-architecture.md](03-final-architecture.md)** — the fully
  detailed version of the architecture: component diagram, state machine,
  data contracts, timing model, storage, failure handling, interfaces, and
  non-functional notes. Kept consistent with `../design.md`; that file is
  the trimmed, build-facing version of this one.
- **[04-open-questions.md](04-open-questions.md)** — product/design
  questions not yet resolved, as distinct from decisions already made in
  Part 1.
- **[05-repo-lifecycle-and-language-coverage.md](05-repo-lifecycle-and-language-coverage.md)**
  — tree-sitter fallback chunking for unsupported/unparseable files, Chroma
  collection keying and staleness, resume-vs-changed-repo behavior, and the
  concrete NFR7 retention policy. Added in response to external design
  review.
- **[06-cli-contract-and-profile-scaling.md](06-cli-contract-and-profile-scaling.md)**
  — the full `viva` CLI command contract (args, flags, exit codes,
  including the previously-undocumented `viva list`), and the recursive
  hierarchical-reduce fallback for Project Profile generation on repos
  with many modules. Added in response to a second external design review.
- **[07-llm-model-pressure-test-results.md](07-llm-model-pressure-test-results.md)**
  — results of the `LLM_MODEL` pressure-test harness
  (`scripts/pressure_test_llm_model.py`), closing
  `04-open-questions.md` item 5: accuracy, classification stability, and
  citation-compliance rate across candidate models (N=10 repetitions per
  sample), which is what `gemma4:e4b`'s default selection is based on.
- **[08-phase-3-analyzer-design.md](08-phase-3-analyzer-design.md)** — the
  Phase 3 Analyzer implementation design: tree-sitter dependency choice,
  the query-per-language extraction mechanism, the `ProjectProfile`
  unification decision, and the golden-repo fixture strategy for the
  hierarchical-reduce path.
- **[09-phase-4-indexing-design.md](09-phase-4-indexing-design.md)** — the
  Phase 4 Indexer/RAG implementation design: why chunk text is re-extracted
  at `INDEXING` rather than threaded from `ANALYZING`, the chunk/metadata
  schema, the `EmbeddingClient` interface, and Chroma collection keying
  and reuse.
- **[10-phase-5-questiongen-design.md](10-phase-5-questiongen-design.md)**
  — the Phase 5 QuestionGen implementation design: the FR12 coverage-plan
  distribution algorithm, the FR13 just-in-time grounded generation flow,
  the query-reformulation + test-path-filter resolution of open question
  #6, and the explicit Phase 5/6 boundary around FR14/FR15 (follow-ups and
  live duplicate tracking are session-state concepts owned by Phase 6).
- **[11-phase-6-session-loop-design.md](11-phase-6-session-loop-design.md)**
  — the Phase 6 Session Loop implementation design: the SQLite session
  schema, the Orchestrator's `start`/`resume` flow, the
  `ClassificationProvider` seam that lets FR14 follow-ups be built for
  real now while staying inert until Phase 7's Evaluator exists, the
  design.md §7 time-budget collapse logic, and known limitations
  (mid-answer timeout interruption, resume scope before `IN_PROGRESS`).

- **[12-phase-7-evaluator-design.md](12-phase-7-evaluator-design.md)** —
  the Phase 7 Evaluator implementation design: retiring the single
  `evaluate_answer` call in favor of a two-call `classify_answer`/
  `generate_feedback` split, the `VectorStore.get_by_ids` addition needed
  to reconstruct ground-truth context from persisted chunk IDs, the
  single-worker-thread-plus-queue backgrounding model (not a thread per
  answer), the four-state `eval_status` model, and how
  `FINALIZING_EVALS`/`viva resume` guarantee no completed evaluation work
  is lost (NFR3).

- **[13-phase-8-report-design.md](13-phase-8-report-design.md)** — the
  Phase 8 Report implementation design: why `ReportBuilder` is a lazy,
  on-demand reader invoked by the `viva report` CLI command rather than
  something the Orchestrator builds during a live session, the
  strengths/weaknesses/topics-to-revisit aggregation rules (including how
  `needs_review` records are excluded from rollups but still surfaced
  individually), the single-`Report`-dataclass Markdown/JSON dual-render
  approach, and the `SUMMARIZING` state's new integrity-check role.

- **[14-phase-9-polish-design.md](14-phase-9-polish-design.md)** — the
  Phase 9 Polish implementation design: an audit of `plan.md`'s Phase 9
  list against what Phases 0–8 already shipped (config validation,
  resume support, and bad-URL/timeout error handling all already done),
  narrowing real scope to `viva cleanup` (NFR7); the survivor-based
  Chroma-collection reference-counting decision (no session still
  pointing at a collection may have it deleted out from under it); and
  why `updated_at`, not `created_at`, is the retention clock.

- **[15-phase-10-web-ui-design.md](15-phase-10-web-ui-design.md)** — the
  Phase 10 Web UI implementation design: `viva serve`, a local FastAPI
  server fronted by a single static HTML+JS page exposing the same
  start/resume/list/report/cleanup operations as the CLI, plus the live
  question/answer loop; the `WebSessionUI`/`queue.Queue` bridge that lets
  `Orchestrator.start()`/`.resume()` keep blocking on
  `SessionUI.read_answer()` from a background thread instead of an HTTP
  request thread, with `Orchestrator` itself unchanged; and the later
  rename from viva-web to viva room alongside its visual redesign.

- **[16-phase-11-voice-io-design.md](16-phase-11-voice-io-design.md)** —
  the Phase 11 Voice I/O implementation design: the faster-whisper/Piper
  engine choices and why the browser's native `SpeechRecognition` API was
  rejected as non-local, the `VoiceIO` thin-interface component and its
  lazy-import testing seam, the `record()`/`transcribe()` split that lets
  spoken answering time count toward the clock while STT/TTS compute is
  excluded (correcting an earlier plan to pause the countdown instead),
  and the CLI-first scope with web UI voice deferred to a follow-up phase.

- **[17-phase-12-web-voice-io-design.md](17-phase-12-web-voice-io-design.md)** —
  the Phase 12 Web Voice I/O implementation design: bringing Phase 11's
  voice mode to "viva room", the server-side-Piper-synthesis and
  raw-PCM-AudioWorklet-capture decisions, the two new endpoints and why
  no per-session backend state was needed for a per-session toggle, and
  the `AnswerTimer` locking fix for the first genuinely cross-thread
  `excluding()` call.

- **[18-phase-13-architecture-tier-questions-design.md](18-phase-13-architecture-tier-questions-design.md)**
  — the Phase 13 Architecture-Tier Questions design: splitting the single
  `architecture` category into an extensible set of topics (overview,
  pipeline, security, integration, concurrency) each capable of multiple
  questions, a separate architecture-tier system prompt permitting
  component/module-level specificity instead of exact-function-level, the
  `phase`-keyed ranking fix that makes architecture questions clear
  before other categories every time the plan is re-ranked (including on
  replenishment), and deriving `max_questions` from session duration with
  live-loop replenishment instead of ending early on `QUESTIONS_EXHAUSTED`.

- **[19-panel-review-findings-2026-09.md](19-panel-review-findings-2026-09.md)**
  — findings from a September 2026 external panel review of the live
  codebase (not just the design docs), grouped by reviewer persona: a
  git-clone URL validation gap that reaches `GitPython`'s `clone_from`
  unchecked, a related `GITHUB_TOKEN` exfiltration path via a tail-anchored
  regex bypass, a stored XSS in the session list's `innerHTML` usage, the
  `serve` command's no-auth-by-default posture, a prompt-injection boundary
  gap between retrieved repo content and system instructions, and the
  `duration_minutes` falsy-zero/negative-value bug. Nothing in it has been
  fixed yet — it's a findings record to turn into a patch series.

- **[20-phase-14-security-hardening-design.md](20-phase-14-security-hardening-design.md)**
  — the Phase 14 implementation design: why §19.4.2, §19.4.3, and §19.5.2
  collapse into a single `validate_repo_url()` replacing `_repo_slug()`
  rather than three separate checks, the new
  `InvalidParametersError(OrchestratorError)` subtype for §19.3.1
  following the existing three-subtype pattern, the exact
  `web/app.py`/`cli.py` exception-to-status-code wiring, why
  `duration_minutes` gets explicit validation instead of a Pydantic
  `Field` constraint (422 vs. this API's documented 400 contract), the
  `web/static/app.js` `textContent` fix for §19.5.1, and the full test
  plan and bisect-safe patch ordering.

- **[21-phase-15-serve-authentication-design.md](21-phase-15-serve-authentication-design.md)**
  — the Phase 15 decision doc for §19.4.1 (`viva serve` has no auth).
  Recommends a shared-secret bearer token, generated per invocation,
  required on `/api/*` only when the bind address isn't loopback — the
  default case is untouched. Weighs and rejects a warning-only flag
  (doesn't actually stop deliberate access) and a full login system
  (solves a multi-user problem this tool doesn't have). Traces every
  network call in `app.js` to identify the three places a token needs
  wiring in (the central `api()` helper, one raw `fetch()` for question
  audio, and two `<a href>` report-download links that can't carry a
  header and need the query-string form instead). Needs sign-off on the
  recommendation before implementation starts.

See also: `../requirements.md` (functional/non-functional requirements)
and `../plan.md` (phased build plan).
