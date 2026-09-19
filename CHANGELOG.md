# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers mark meaningful milestones rather than a stability
promise -- this project is pre-1.0 and, per the README, "not yet ready
for general use."

## [Unreleased]

## [0.3.0] - 2026-09-19

Everything closing out a September 2026 external panel review
(`docs/system-design/19-panel-review-findings-2026-09.md`): Phases
14-17 in full, plus two real bugs found via actual timed sessions
against real repos while validating them (not caught by the test suite)
-- full write-ups in the phase design docs referenced below.

### Security

- Fixed a `git clone` URL validation gap: a crafted `repo_url` could
  satisfy the old validation regex while resolving to a different host
  under actual URL parsing, letting `GITHUB_TOKEN` be sent to the wrong
  host, and nothing restricted the URL scheme to `{https, ssh}` before
  it reached `git clone`, leaving an unrestricted git transport
  reachable from user input. See
  [`docs/system-design/19-panel-review-findings-2026-09.md`](docs/system-design/19-panel-review-findings-2026-09.md)
  §19.4.2/§19.4.3 and
  [`docs/system-design/20-phase-14-security-hardening-design.md`](docs/system-design/20-phase-14-security-hardening-design.md).
- Fixed a stored XSS in `viva serve`'s session list: a session's
  `repo_url` was rendered via `innerHTML` instead of `textContent`, so
  a crafted value would execute in the browser of anyone viewing the
  list (§19.5.1).
- `viva serve` now requires a shared-secret access token on every
  `/api/*` request once bound to a non-loopback address (e.g.
  `--host 0.0.0.0`) -- the default, loopback-only case is unaffected.
  A follow-up fix closed a real gap found during manual testing of
  this: the page shell at `/` was embedding the real token into every
  response regardless of whether the request already supplied it,
  letting anyone who loaded the bare URL read the token straight out of
  the page source and use it for every subsequent `/api/*` call. See
  [`docs/system-design/21-phase-15-serve-authentication-design.md`](docs/system-design/21-phase-15-serve-authentication-design.md).
- Added an instruction-injection boundary to all four LLM system
  prompts (question generation, architecture questions, answer
  classification, feedback): retrieved code and the candidate's own
  spoken answer are now explicitly described as data to reason about,
  never instructions to follow, even if phrased as one -- closing a gap
  where a candidate could plant a comment in their own repo (or say
  something in an answer) attempting to talk the grader into marking
  any answer correct. Confirmed live against the real configured model
  with a deliberately adversarial docstring and a deliberately wrong
  answer (`not_attempted`, not `correct`). See §19.1.1.

### Added

- A retrieval-quality distance filter: a retrieved code chunk whose
  distance from the query exceeds `MAX_RETRIEVAL_DISTANCE` (default
  `0.85`, informed by real session data across three repos and six
  question categories, not a guess) is dropped, and a question left
  with nothing well-grounded to ask about is skipped rather than asked
  anyway. Every retrieval now logs its outcome (category, topic,
  module, file, fetch/filter counts, min/max distance) for
  diagnosability. See §19.1.2/§19.6.2 and
  [`docs/system-design/22-phase-16-grading-integrity-observability-design.md`](docs/system-design/22-phase-16-grading-integrity-observability-design.md).
- CLI logging hygiene: `httpx`/`httpcore`'s per-request trace and the
  retrieval-quality log line above no longer print to the live session
  terminal -- both were flooding the question/answer UI, one line per
  Ollama call and one per question. Redirected to a per-day file
  instead (`logs/log_<YYYY_MM_DD>.log`), with a 3-day retention sweep
  at CLI startup. Not a removal -- the trace still has real debugging
  value, it's just off-screen during an actual session now.

### Fixed

- `duration_minutes=0` sent to `viva start` or the web API no longer
  silently falls back to the configured default; `0` and negative
  values are now rejected outright (§19.3.1).
- A malformed `repo_url` is now rejected before a session row is
  persisted, instead of surfacing several layers downstream (§19.5.2).
- A follow-up question to an architecture question was silently losing
  its architecture topic, collapsing every such follow-up -- regardless
  of which of the five real topics (overview/pipeline/security/
  integration/concurrency) its parent was actually about -- into one
  generic, always-identical retrieval query. Found live: 5 of 14
  questions in one real session came back as literal duplicates.

## [0.2.0] - 2026-09-13

Phase 13 (architecture-tier questions) plus three real-world fixes found
while validating it against timed sessions, and a CI change.

### Added

- Architecture-tier questions: the `architecture` category is now spread
  across an extensible set of topics (system overview, pipeline/data
  flow, security boundaries, external integrations, concurrency) instead
  of a single guaranteed slot, each capable of holding more than one
  question. Every architecture-topic question is asked ahead of the
  other four categories in a session, rather than interleaved with them
  from question one -- previously `architecture` shared a system prompt
  with `implementation_detail` that demanded exact-function-level
  specificity, so real architecture questions came out reading like "why
  did you use this particular line of code" instead of "how does X flow
  through Y."
- The question budget now scales with each session's own chosen
  duration (roughly one question per two minutes) instead of a flat
  default, and a session that gets answered faster than expected has its
  plan extended rather than ending early with real time still on the
  clock. An explicit `MAX_QUESTIONS` still overrides this regardless of
  session duration, for anyone who wants a fixed count.
- CI now tests Python 3.11, 3.12, and 3.13 across both Ubuntu and
  Windows -- one Python version at a time (each version's two-OS pair
  runs in parallel with itself, but the next version doesn't start until
  the current one finishes).

### Changed

- Widened `rich` from `>=13.7,<14.0` to `>=13.7,<16.0` (Dependabot).
- Widened `pytest` (dev extra) from `>=8.0,<9.0` to `>=8.0,<10.0`
  (Dependabot).
- Bumped `actions/checkout` from `v4` to `v7` and `actions/setup-python`
  from `v5` to `v7` in `tests.yml` and `release.yml` (Dependabot).

### Fixed

Three real-world bugs found via actual timed sessions against real
repos (not caught by the test suite) while validating the duration-based
question budget above -- full write-up in
`docs/system-design/18-phase-13-architecture-tier-questions-design.md`
§18.7-§18.8:

- The question budget was being derived from the server process's
  global default duration at startup, not the duration actually chosen
  for an individual session -- a 5-minute session picked in the browser
  could still plan a 30-minute-sized set of questions.
- `.env.example` shipped `MAX_QUESTIONS` set explicitly (uncommented),
  so the standard "copy the example file" setup step silently opted
  every fresh install out of duration-based scaling entirely, with
  nothing indicating why.
- There was no logging configuration anywhere in the codebase -- even
  after the two fixes above, there was no way to see from outside the
  process what duration or question-budget decision a session had
  actually made.

## [0.1.0] - 2026-09-11

First tagged release. Everything below shipped incrementally across
Phases 0-12 (see `docs/plan.md` for the phase-by-phase build order and
`docs/system-design/` for the design doc behind each one); this entry
groups it by what it does rather than when it landed.

### Added

**Core pipeline**
- Clone and sample a GitHub repo (up to 500 files, representative
  sampling across modules for larger projects), analyze it into a
  project-level architecture summary via map-reduce, and index it for
  retrieval using AST-based code chunking (tree-sitter) and local
  embeddings -- no external API calls anywhere in this path.
- Grounded question generation: a three-pass coverage planner produces
  questions cited against real file/function locations, not generic
  prompts.

**The viva session itself**
- A live, timed Q&A loop (`viva start`) with a countdown that only
  counts answering time -- LLM generation and evaluation latency are
  explicitly excluded from the clock at every call site that needs it.
- Adaptive follow-up questions on weak answers, bounded by a
  configurable depth.
- Grounded evaluation: every "you missed this" or "this was wrong"
  verdict cites the specific code it's based on; criticism that isn't
  grounded in the actual repo is discarded rather than shown.
- Crash-resumable sessions (`viva resume`) -- session state persists
  continuously, including answer-time already spent.

**Reporting and session management**
- Structured per-question feedback (summary, what went well, what was
  missed, what was wrong) and a full session report in Markdown, JSON,
  or HTML (`viva report`).
- `viva list` and `viva cleanup` (retention-based, matching NFR7) for
  managing sessions and their indexed data over time.

**Voice I/O, CLI and browser**
- Speak your answers and have questions read aloud, entirely local
  (faster-whisper for transcription, Piper for synthesis) -- opt-in via
  `VOICE_ENABLED=true`, with `viva voice setup` to pre-pull both models.
- The same voice mode in "viva room" (see below): a per-browser-session
  toggle, spoken questions played back as audio, and spoken answers
  captured via raw PCM (not a lossy codec) and transcribed for review
  before submitting -- never auto-submitted sight-unseen.
- Automatic fallback to typed input at every point voice can fail
  (silence, an unavailable model, a hardware error) -- voice mode
  degrades gracefully rather than blocking the session.

**"viva room" -- the browser interface**
- `viva serve` runs a local FastAPI server exposing the same
  start/resume/list/report/cleanup operations as the CLI, plus the live
  question/answer loop, fronted by a single static HTML+JS page with no
  frontend framework or build step.
- A two-column report view with session metadata in a sticky sidebar
  alongside the report body, rather than one long centered column.

### Fixed

A representative sample of real-world bugs found via actual hardware/
browser testing (not just mocked unit coverage) rather than assumed
away -- full write-ups live in the relevant design doc's own
"real-world bugs found during testing" section:
- An incompatible-GPU crash in the STT engine, fixed by forcing CPU
  inference rather than relying on automatic hardware selection.
- A missing live countdown during voice recording in the CLI, and a
  mismatched (non-live) one in the browser -- both now match the
  typed-input experience.
- Voice answers being submitted before there was any chance to review
  or correct a misheard word.
- A packaging bug caught while setting up this release: the built wheel
  silently dropped `viva/web/static/*` entirely, which would have made
  `viva serve` unusable for anyone installing this package for real
  (as opposed to an editable dev install, which reads straight from
  the source tree and never exercised this path).

### Known limitations

- Early build stage -- see `docs/plan.md`. Not yet ready for general
  use.
- Web voice mode requires a secure context (HTTPS or `localhost`) in
  the browser; this is a platform restriction, not something this
  project can route around.
- No license has been chosen yet (`pyproject.toml`'s `license` field is
  a placeholder).
