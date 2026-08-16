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
- **Exit criteria:** `python -m viva --version`-style smoke test passes; config loads and validates required env vars.

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
- Stretch: simple web UI.

## Cross-Cutting: Testing
- A small fixture set of real "golden repos" (a few small, varied-language
  repos) is checked into the test suite from Phase 2 onward and reused
  across Phases 2–8, so profile/question/evaluation quality regressions are
  visible in CI rather than only caught by manual eyeballing.
- Each phase's exit criteria should be re-run against the full golden-repo
  set, not just the repo used during that phase's development.
