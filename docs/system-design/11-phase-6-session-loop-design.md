# System Design Reference — Part 11: Phase 6 Session Loop Design

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts.

## 11.1 Scope

FR16–FR20 (timed session mechanics) plus the CLI contract's `start` /
`resume` / `list` commands (§6.1). This is the first phase with an
Orchestrator, and the first with SQLite persistence — everything before
this phase ran as one synchronous pipeline call per component with no
state surviving process exit.

**No evaluation.** `docs/plan.md` scopes Phase 6 to "Session Loop... no
evaluation yet." The Evaluator (FR21–24) is Phase 7. Every persisted Q&A
record's `eval_status` is `"deferred"` in this phase — see §11.4.

New modules: `src/viva/storage/` (`schema.py`, `session_store.py`),
`src/viva/orchestrator.py`, `src/viva/session_ui.py`,
`src/viva/classification.py`. `cli.py` gains `start`/`resume`/`list`.

## 11.2 Storage

Plain `sqlite3`, no ORM — consistent with the project's existing
convention of thin direct-client wrappers (Chroma via
`indexer/store.py`, Ollama via `llm_client.py`) rather than a heavier
abstraction layer, and the schema is small and stable enough not to
need one.

Two tables, `sessions` and `qa_records`, mapping onto design.md §6's
Project Profile / Q&A Record contracts. `SessionStore`
(`storage/session_store.py`) is the single interface — per design.md's
"no service calls another directly" rule, nothing outside `storage/`
touches `sqlite3` or raw SQL.

`sessions.status` holds the design.md §2 state machine values
(`INGESTING` → `ANALYZING` → `INDEXING` → `PLANNING` → `IN_PROGRESS` →
`TIME_EXPIRED`/`QUESTIONS_EXHAUSTED` → `FINALIZING_EVALS` →
`SUMMARIZING` → `COMPLETE`), plus one pragmatic addition not in that
diagram: **`FAILED`**, for a session that errors out before reaching a
real terminal state (e.g. a clone failure during `INGESTING`) — without
it, such a session would sit forever in a non-terminal status that
`viva resume` would then try (and fail) to resume into. Not enforced as
a SQL `CHECK` constraint — Phase 7/8 will likely add their own states,
so validity is enforced at the Python layer instead of baked into the
schema.

`qa_records.status` tracks each plan item's own lifecycle — `pending` →
`asked` → `answered`, or `skipped_no_grounding` / `skipped_time_collapse`
if it's dropped before ever being asked (§11.5). This is deliberately
separate from `eval_status`, which Phase 6 never sets to anything but
`"deferred"` (§11.4).

`session_id` is a short `uuid4` hex (12 chars) — created and persisted
(status `INGESTING`) *before* cloning starts, so `viva start` can print
it to stdout immediately per CLI contract §6.1, well before
`repo_slug`/`commit_sha` are known. Those get backfilled via
`set_pipeline_artifacts()` once Ingest/Indexing actually resolve them.

The persisted `ProjectProfile` (needed for resume, since re-running
Ingest/Analyzer would re-touch the network — forbidden by
`05-repo-lifecycle...` §5.3) is **not** stored in SQLite; it's written
as JSON next to the session DB (`{session_id}-profile.json`) via new
`ProjectProfile.save()`/`.load()` methods, with `sessions.profile_path`
pointing at it. All of `ProjectProfile`'s nested dataclasses are already
JSON-primitive except `local_path` (a `Path`), which is stringified.

## 11.3 Orchestrator

`Orchestrator` (`orchestrator.py`) is the first place multiple
components get called by one caller — Ingest → Analyzer → Indexer →
QuestionGen in sequence, mediating exactly as design.md's component rule
describes. Two public entrypoints: `start()` and `resume()`.

`start()`:
1. Create the session row (status `INGESTING`), hand `session_id` to the
   UI immediately.
2. Run Ingest → Analyzer → Indexer synchronously, updating `status` after
   each (`ANALYZING`, `INDEXING`), persisting the Project Profile and
   `set_pipeline_artifacts()` once indexing completes.
3. `PLANNING`: call `build_coverage_plan()`, persist every item as
   `pending` via `save_plan()`.
4. Hand off to the live loop (§11.5), which ends at `COMPLETE`.

Any exception during steps 2–3 sets the session `FAILED` with the error
message and re-raises for the CLI to report (exit code 1).

`resume()`:
1. Look up the session. Errors (CLI contract §6.1, exit code 3) if it
   doesn't exist, is already `COMPLETE` (points to `viva report`
   instead), is `FAILED`, or crashed before reaching `IN_PROGRESS` — see
   §11.6 for why that last case is out of scope for this phase.
2. Reload the persisted `ProjectProfile` from `profile_path`.
3. Reconstruct elapsed answer time from persisted `asked_at`/`answered_at`
   timestamps on already-answered records (best-effort — see §11.6) and
   restart `AnswerTimer` with that offset via its new
   `start(initial_elapsed_seconds=...)` parameter.
4. Resume the live loop, which reads pending items straight from
   `SessionStore` rather than needing a plan passed in.

## 11.4 The FR14/Evaluator boundary: `ClassificationProvider`

design.md §7 ties the follow-up decision to the fast classification call
("whether/how to generate a follow-up depends on it"), but that call
belongs to the Evaluator, which doesn't exist until Phase 7.

**Decision (confirmed before implementation): build the full follow-up
mechanism now, gated behind a `ClassificationProvider` seam that always
returns `None` in Phase 6.**

`classification.py` defines the ABC (mirrors the `LLMClient`/
`EmbeddingClient` pattern, NFR5) and `NullClassificationProvider`, the
one implementation Phase 6 injects. The Orchestrator's follow-up branch
(`_maybe_queue_followup`) is real, tested code — not a `TODO` — but with
`NullClassificationProvider` it structurally never fires: every
`classify()` call returns `None`, so no follow-up is ever queued, and
every `qa_records.eval_status` lands `"deferred"`. Phase 7 swaps in a
real provider backed by the synchronous classification call; the
Orchestrator's control flow doesn't change.

The alternative considered — skip the follow-up mechanism entirely and
add it in Phase 7 — was rejected because `add_followup_item()`,
`_followup_depth()` (bounded by `MAX_FOLLOWUP_DEPTH`), and the plan-item
`is_followup_of` threading all touch the same storage/orchestrator code
Phase 7 would otherwise have to retrofit; building and testing the
mechanism now, inert, is cheaper than re-opening this code later.

## 11.5 The live loop (`IN_PROGRESS`)

Each iteration:
1. If the timer's expired, transition to `TIME_EXPIRED` and stop.
2. If there are no pending items left, transition to `QUESTIONS_EXHAUSTED`
   and stop.
3. Pick the next item (`_select_next_item`, below).
4. Generate its question (`questiongen.generate_question()`, wrapped in
   `timer.excluding()` so LLM latency never counts against the clock —
   FR17). If ungrounded, mark `skipped_no_grounding` and loop again
   without asking.
5. Persist `asked`, show the question via `SessionUI.ask_question()`,
   block on `SessionUI.read_answer()`, persist the answer as `answered`.
6. `_maybe_queue_followup()` (§11.4 — inert in Phase 6).

**Time-budget collapse** (design.md §7: "remaining_time /
avg_time_per_remaining_category collapses to one question per remaining
category"): before picking the next item, `_select_next_item` computes
`len(categories_remaining) * AVG_TIME_PER_CATEGORY_SECONDS` (new tunable,
default 180s, FR28) and compares it to `timer.remaining()`. If the
budget doesn't fit, every pending item beyond the first-per-category is
marked `skipped_time_collapse` (not silently dropped — visible in
`viva list`/the eventual report) rather than asked, favoring full
category coverage over depth as the repo review specified. Follow-up
items, when present, are always prioritized first (probing a weak
answer takes precedence over new coverage) — dead code in Phase 6 per
§11.4, live in Phase 7.

Once the loop ends (either exit), the state machine passes straight
through `FINALIZING_EVALS` → `SUMMARIZING` → `COMPLETE` — both are
no-ops in Phase 6 (nothing to finalize, no report to build), kept as
real transitions so Phase 7/8 only have to fill in behavior at an
already-correct point in the sequence, not add new states.

## 11.6 `SessionUI`, and a known limitation

`SessionUI` (`session_ui.py`) is the Orchestrator's interface to
whatever's presenting the session to a person — same seam pattern as
`ClassificationProvider`. `RichSessionUI` is the one real implementation:
`rich.live.Live` renders `AnswerTimer.format_remaining()` on a 0.5s
refresh from the main thread while a background thread blocks on
`sys.stdin.read()` for the answer, terminated by EOF (Ctrl-D / Ctrl-Z),
per the multi-line-answer decision made before this series started. In
tests, the Orchestrator is driven by a scripted fake UI instead — no
real terminal or threads involved.

**Known limitation:** a blocking `sys.stdin.read()` on a background
thread can't be forcibly interrupted from the main thread. If the timer
expires mid-answer, the countdown display stops and a "time's up"
notice prints, but the read itself keeps blocking until the person
actually presses Ctrl-D — whatever they'd typed by then is still
captured as the answer. This doesn't affect FR17's actual guarantee
(LLM/eval latency exclusion from the clock, tested since Phase 0); it
only affects how promptly typing is cut off once time runs out. Flagging
this now rather than after the fact — a cleaner fix (e.g. a
`select()`-based read with a timeout, POSIX-only) is a reasonable follow-up
if this turns out to matter in practice.

**Known scope-narrowing on resume:** `resume()` only handles sessions
that already reached `IN_PROGRESS`. A session that crashes during
`INGESTING`/`ANALYZING`/`INDEXING`/`PLANNING` has nothing durable to
resume from (no persisted Project Profile yet) and errors clearly
instead of attempting a partial pipeline replay — the CLI message points
back to `viva start`. NFR3 ("a crash... at any point after `INGESTING`
must not lose already completed work") is satisfied for the case that
actually matters in practice (an interrupted *viva*, not an interrupted
*setup*), but this is narrower than a literal reading of "at any point,"
flagged here for visibility rather than silently narrowed.

## 11.7 New config

`SESSION_DB_PATH` (default `./data/viva.db`) and
`AVG_TIME_PER_CATEGORY_SECONDS` (default `180`) — per FR28, a tunable
estimate rather than a hardcoded guess, since answer pacing varies a lot
by person and by repo complexity.

## 11.8 Explicitly out of scope (per `docs/plan.md`)

Real evaluation (Phase 7), `viva report` (Phase 8), `viva cleanup`
(Phase 9).
