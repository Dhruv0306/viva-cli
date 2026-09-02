# System Design Reference — Part 13: Phase 8 Report Design

## 13.1 Scope

FR25–FR27: aggregate per-question `EvaluationRecord`s (Phase 7) into a
final report, in Markdown (default) or JSON, via a new `viva report`
command. Phase 8 adds no new orchestrator states and no new
`qa_records`/`sessions` columns; it's a read path over data Phase 6/7
already persist.

Explicitly out of scope (per `docs/plan.md`): `viva cleanup` (Phase 9),
any change to evaluation logic itself (Phase 7, done), and a web UI
(Phase 9 stretch).

## 13.2 `ReportBuilder` is a reader, not a session-time writer

`docs/design.md`'s component diagram hangs `ReportBuilder` off
`Evaluator`, but nothing about report generation is time-sensitive or
session-local (the same property `12-phase-7-evaluator-design.md` §12.6
already leans on for resume-time feedback re-derivation). `ReportBuilder`
is invoked lazily, on demand, by the `viva report` CLI command — not by
the Orchestrator during a live session. This mirrors the existing
`list_sessions` CLI command, which reads straight from `SessionStore`
without going through the Orchestrator at all, rather than the
`start`/`resume` commands, which do because those genuinely drive a
live state machine.

Rationale: a session-time-generated report would need a place to live
(a new column, a file on disk) and a staleness story (what if a report
is regenerated after `--allow-partial` was used, then the session later
completes for real?). Building it on demand from `qa_records` avoids
both — the source of truth stays exactly where it already is.

`ReportBuilder.build(session: SessionRecord, qa_records: list[QARecordRow]) -> Report`
lives in `src/viva/report.py`, takes what `SessionStore.get_session()` /
`get_qa_records()` already return, and has no I/O of its own — the CLI
command owns reading from `SessionStore` and writing the rendered output.
This keeps `ReportBuilder` trivially unit-testable against plain
dataclasses, consistent with the project's single-public-entrypoint,
no-cross-component-imports convention (`docs/design.md` §1).

## 13.3 `SUMMARIZING` becomes an integrity check, not a report build

The orchestrator's `SUMMARIZING → COMPLETE` transition
(`orchestrator.py`, currently a passthrough with a `# Phase 6: no report
generation yet (Phase 8)` comment) is *not* where `ReportBuilder` gets
called, per §13.2. What it does get, in Phase 8:

After `FINALIZING_EVALS`'s `Evaluator.flush()` call, every `qa_record`
that was ever `asked`/`answered` should be at a terminal `eval_status`
(`complete` or `needs_review`) — `flush()`'s bounded timeout already
guarantees this per `12-phase-7-evaluator-design.md` §12.6. `SUMMARIZING`
adds one defensive pass: any answered record still found at
`classified` or `feedback_pending` (which should be unreachable given
`flush()`'s contract, but costs nothing to guard) is forced to
`needs_review` via the existing `SessionStore.mark_eval_needs_review()`
before the transition to `COMPLETE`. This makes `COMPLETE` a hard
guarantee — "every answered question has a terminal eval_status" — that
`ReportBuilder` and FR27's "unfinished evaluation must complete before
the report is generated, not be dropped" can both rely on without
re-checking it themselves.

## 13.4 Aggregation logic

`ReportBuilder.build()` walks `qa_records` (only `status in ("answered",)`
— `pending`/`skipped_*` rows never got an answer and are reported
separately as a coverage note, not folded into strengths/weaknesses) and
produces:

```python
@dataclass(frozen=True)
class ReportSection:
    heading: str
    items: list[str]

@dataclass(frozen=True)
class QuestionSummary:
    question_id: str
    category: str
    question_text: str | None
    classification: str  # Classification | "needs_review"
    summary: str
    needs_review: bool

@dataclass(frozen=True)
class Report:
    session_id: str
    repo_slug: str | None
    commit_sha: str | None
    status: str
    generated_at: str
    total_questions: int
    answered_count: int
    classification_counts: dict[str, int]
    strengths: list[str]
    weaknesses: list[str]
    topics_to_revisit: list[str]
    needs_review_count: int
    questions: list[QuestionSummary]
```

- **Strengths**: `did_well` entries from records classified `correct` or
  `partial`, deduplicated by exact text (case-insensitive), capped —
  the same "don't drown a human reader in near-duplicate model output"
  concern that motivated Phase 7's small-schema-per-call split
  (`01-resolved-decisions.md` §1.2) applies here too, so a length cap
  (`REPORT_MAX_ITEMS_PER_SECTION`, see §13.6) is applied per section
  after dedup rather than left unbounded.
- **Weaknesses**: `missed` + `did_wrong` entries (as
  `f"{point} ({cited_file})"` when `cited_file` is present, else just
  `point`) from records classified `partial`/`incorrect`, same dedup/cap.
- **Topics to revisit**: `category` values from records classified
  `partial`/`incorrect`/`needs_review`, ranked by frequency (ties broken
  by first-appearance order, matching `qa_records`' implicit rowid
  ordering that Phase 5/6 already rely on elsewhere) — a category name,
  not a re-statement of individual question feedback.
- **`needs_review` records** are counted (`needs_review_count`) and
  included in `questions` like any other record, but their
  `did_well`/`missed`/`did_wrong` text is *excluded* from strengths/
  weaknesses aggregation — an unsubstantiated verdict (FR22's whole
  reason for the `needs_review` flag existing) shouldn't be laundered
  into an aggregate strength/weakness statement just because it survived
  to this stage. This resolves the open design question from the
  previous conversation: `needs_review` items surface individually (in
  the per-question table, and in the `needs_review_count` summary line)
  but never contribute to the rolled-up strengths/weaknesses/topics
  lists.

## 13.5 Two render paths

`render_markdown(report: Report) -> str` and `render_json(report: Report) -> str`
are separate pure functions over `Report`, not two branches inside one
function — same "each piece stays close to one concern" preference
Phase 7 applied to schema design. `render_json` uses
`dataclasses.asdict()` directly: **`Report`'s shape *is* the JSON
schema** (resolving the second open question from the previous
conversation) rather than a separate hand-maintained schema mirroring
`EvaluationRecord` — `Report` is already a purpose-built aggregate, not
a pass-through of `EvaluationRecord`, so there's no independent shape to
keep in sync with; one dataclass, two renderers.

`render_markdown` produces: a header (repo, commit, status, generated-at
timestamp), a one-line coverage summary (`answered_count`/
`total_questions`, `needs_review_count`), then `## Strengths`,
`## Weaknesses`, `## Topics to Revisit` sections (each rendered as a
bullet list; a section with no items renders as `_None noted._` rather
than being omitted, so the report's shape is stable across sessions),
then a `## Question-by-Question` table (category, classification,
summary — full per-question `did_well`/`missed`/`did_wrong` detail is
intentionally left out of the table to keep the top-level report
scannable; `--format json` is the path for consumers that want full
per-question detail).

## 13.6 New config (FR28: env-driven, no hardcoding)

- `REPORT_MAX_ITEMS_PER_SECTION` (default `10`) — cap applied to
  strengths/weaknesses after dedup (§13.4). Topics-to-revisit is not
  capped by this — it's already bounded by the number of distinct
  categories in use, typically small (FR12's category set).

## 13.7 `viva report` CLI command

Follows the CLI contract (`06-cli-contract-and-profile-scaling.md` §6.1)
exactly: `viva report <session_id> [--format md|json] [--output <path>]
[--allow-partial]`.

```
session = store.get_session(session_id)
if session is None: exit(3)   # not found
if session.status != "COMPLETE" and not allow_partial: exit(3)
qa_records = store.get_qa_records(session_id)
report = ReportBuilder().build(session, qa_records)
text = render_json(report) if format == "json" else render_markdown(report)
write to --output path, or print to stdout
```

Reuses `SessionNotFoundError`-style handling already established for
`resume` — `viva report` on an unknown `session_id` and `viva report` on
a not-yet-`COMPLETE` session without `--allow-partial` are both exit
code 3, matching `resume`'s existing "valid input, not actionable"
convention rather than inventing a new error class per case.

## 13.8 Migration / blast radius

- `src/viva/report.py` — new: `ReportSection` (unused directly by
  `Report` but kept as the section-list building block used internally
  by the aggregation helpers), `QuestionSummary`, `Report`,
  `ReportBuilder`, `render_markdown`, `render_json`.
- `src/viva/config.py` — add `report_max_items_per_section` (FR28,
  §13.6), `.env.example` updated in the same patch.
- `src/viva/orchestrator.py` — `SUMMARIZING` transition gets the
  integrity-check pass (§13.3); no new states, no new
  `classification_provider` calls beyond the existing `flush()`.
- `src/viva/cli.py` — new `report` command (§13.7).
- `README.md` — `viva report` usage example promoted from illustrative
  to accurate (it was already mentioned per
  `06-cli-contract-and-profile-scaling.md`'s original motivation, but
  never implemented until now).
- New tests: `tests/test_report.py` (aggregation + both renderers,
  pure-dataclass fixtures, no DB/LLM), `tests/test_cli_report.py`
  (CLI contract: exit codes, `--format`, `--output`, `--allow-partial`,
  `viva list` → `viva report` happy path against a real `SessionStore`).
  `tests/test_orchestrator.py` gets one new case: an answered record
  artificially left at `feedback_pending` past `flush()` is forced to
  `needs_review` by `SUMMARIZING` before `COMPLETE`.

## 13.9 Real-world bugs found during Phase 8 testing

**Unanswered questions silently disappeared from the report.** Found
running a real `viva start https://github.com/Dhruv0306/throttle4j
--duration 8` session on Windows: the session's time budget ran out
with 10 questions answered and 1 planned qa_record left at `PENDING`,
never reached. `viva report` showed `"total_questions": 11,
"answered_count": 10"` but gave no indication anywhere — not in the
Markdown table, not in the JSON `questions` array — of what happened to
the 11th question. §13.4 of this very doc had already specified that
`pending`/`skipped_*` records "are reported separately as a coverage
note, not folded into strengths/weaknesses," but the first
implementation only did the first half (excluding them from the
strengths/weaknesses walk) and dropped the second half (the note
itself) entirely — the same "read half the sentence, implement half the
sentence" mistake as Phase 6's FR15 bug (§11.9 of the Phase 6 doc).

Fixed: `Report` gained a `coverage_notes: list[str]` field.
`ReportBuilder.build()` now groups every non-`answered` `qa_record` by
its `status` (`pending`, `skipped_time_collapse`,
`skipped_duplicate_target`, `skipped_no_grounding`) and renders one
human-readable, pluralized sentence per reason present (e.g. "1
question planned but not reached before the session ended."),
in both Markdown (indented under the `Answered:` line) and JSON. An
unanswered record is still never counted in `classification_counts`,
`strengths`, `weaknesses`, or `topics_to_revisit` — the fix only adds
the missing visibility, it doesn't change what counts as answered.

Regression test: `test_coverage_notes_surface_unanswered_records_by_reason`
in `tests/test_report.py` reproduces the exact 10-answered/1-pending
shape from the real session.

## 13.10 Explicitly out of scope

- `viva cleanup` (Phase 9).
- Any report caching/persistence — every `viva report` call re-aggregates
  from `qa_records` fresh (§13.2).
- Web UI rendering of report data (Phase 9 stretch, per `docs/plan.md`).
