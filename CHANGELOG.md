# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers mark meaningful milestones rather than a stability
promise -- this project is pre-1.0 and, per the README, "not yet ready
for general use."

## [Unreleased]

### Changed

- Widened `rich` from `>=13.7,<14.0` to `>=13.7,<16.0` (Dependabot).
- Widened `pytest` (dev extra) from `>=8.0,<9.0` to `>=8.0,<10.0`
  (Dependabot).
- Bumped `actions/checkout` from `v4` to `v7` and `actions/setup-python`
  from `v5` to `v7` in `tests.yml` and `release.yml` (Dependabot).

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
