# System Design Reference — Part 18: Phase 13 Architecture-Tier Questions Design

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts.

## 18.1 Scope

Real-session feedback: `architecture`-category questions came out
indistinguishable from `implementation_detail` ones ("why did you use this
particular line of code"), because a single system prompt and a single
guaranteed plan slot served both. This phase makes `architecture` a
distinct tier, spread across multiple topics, asked before the other four
categories, with its own prompt register. It also fixes an
already-latent problem this change would otherwise make worse: the
question budget didn't scale with session length, so a fast answerer
could exhaust the plan with time still on the clock.

Four things change, each isolated to one seam so they can land (and be
bisected) independently:

1. `architecture` becomes topic-scoped, multiple questions per topic
   (§18.2).
2. Architecture-tier questions get their own prompt register, permitting
   component/module-level specificity instead of exact-function-level
   (§18.3).
3. Session-loop ranking gains a `phase` key so every architecture
   question clears before any other category starts, and re-applies on
   every ranking call rather than being a one-time ordering decision
   (§18.4).
4. `max_questions` is derived from `VIVA_DURATION_MINUTES` when not
   explicitly set, and the live loop replenishes the plan on exhaustion
   instead of ending early (§18.5).

## 18.2 Topic-Scoped Architecture Coverage (FR12)

`QuestionCategory` stays exactly as it is — five categories, no schema
change, no `report.py`/`storage/schema.py` migration. Splitting
`architecture` into topics happens one level below the category, via a
new field:

```python
@dataclass(frozen=True)
class QuestionPlanItem:
    id: str
    category: QuestionCategory
    target_module: str | None
    target_file: str | None = None
    architecture_topic: str | None = None  # NEW
    status: PlanItemStatus = "pending"
    is_followup_of: str | None = None
```

Invariant (documented, not enforced — matching the module's existing
style of trusting `QuestionCategory` as a `Literal` by convention rather
than validating it): `architecture_topic` is set if and only if
`category == "architecture"`.

`retrieval.py` gets an open, extensible topic registry, replacing the
single `"architecture"` entry in `_CATEGORY_QUERY_TEMPLATES`:

```python
_ARCHITECTURE_TOPICS: dict[str, str] = {
    "overview": "the overall structure and how the major components fit together",
    "pipeline": "the stages data or a request passes through end to end, from entry point to output",
    "security": "authentication, authorization, input validation, secrets handling, and trust boundaries",
    "integration": "external services, APIs, or dependencies the system integrates with",
    "concurrency": "threading, async, locking, or shared-state coordination",
}
```

Adding a topic later is a dict entry — no branching logic anywhere else
needs to change. A topic that returns zero grounding chunks (e.g.
`security` on a repo with no auth code) is skipped through the existing
`generate_question() -> None` path; no new skip mechanism needed.

`planner.py`'s Pass 1 changes from "one slot for `architecture`" to "one
slot per architecture topic". A new pass (1.5, between the existing
Pass 1 and Pass 2) gives each topic **one additional** question before
Pass 2 spends any budget on per-module categories — deliberately capped
at one extra round rather than an unbounded round-robin. A topic has no
natural exhaustion the way a `(category, module)` pair does (there's
always another question you could ask about "pipeline"), so an uncapped
loop here would consume the entire remaining budget on architecture
alone and starve Pass 2/3 completely, which isn't what "prefer more
architecture depth" was meant to produce. This surfaced during
implementation, not during design review — recorded in §18.6 alongside
the panel-review findings, same discipline either way. Revisit as a
config knob (e.g. `ARCHITECTURE_EXTRA_ROUNDS`) if one extra question per
topic proves too thin in practice.

**Dedup key fix (found in panel review before implementation, not after
— see §18.6):** `_add()`'s dedup key is `(category, target_module,
target_file)`. Every architecture item has `target_module=None,
target_file=None`, so a second architecture item collides with the first
in the `seen` set and silently gets dropped. The key becomes
`(category, target_module, target_file, architecture_topic)`.

## 18.3 Two-Tier Prompting (FR13)

`llm_client.py`'s `QUESTION_GEN_SYSTEM_PROMPT` stays exactly as-is and
keeps serving `implementation_detail` / `tech_choice_rationale` /
`error_handling` / `testing_strategy` — that prompt's insistence on an
exact function/class/parameter is correct for that tier, it was just
wrongly being applied to `architecture` too.

A new `ARCHITECTURE_QUESTION_GEN_SYSTEM_PROMPT` serves `architecture`
only. Same one-clause/15-25-word/no-preamble constraints, but the
specificity requirement changes noun: "naming the exact component,
module, or pipeline stage the context shows" instead of "naming the
exact function/class/parameter" — specific enough to stay grounded and
non-generic, without forcing a line-level drill-down that isn't what an
architecture question is for.

New example pair, since reusing the implementation-tier examples risks
the model anchoring on "name the exact function" phrasing even after the
instruction text above it changes:

> Good: "How does a sampled file get handed off from Ingest to the
> Analyzer's map step?"
> Bad (too line-level — that's the other tier's job): "Why does
> `_top_level_module` return an empty string for root-level files?"

`generate_question()` in `questiongen/__init__.py` picks the system
prompt by category, and passes `architecture_topic` through as a new
`[ARCHITECTURE_TOPIC]` labeled section in the user prompt (same
explicit-non-concatenated-sections convention as every other prompt in
`llm_client.py`), so the model knows which lens it's writing for when one
system prompt now serves five-plus topics at once.

`avoid_questions` needs no change — it's already passed session-wide
regardless of category, so a `pipeline`-topic question already sees prior
`overview`-topic questions in `[AVOID_REPEATING]` and won't ask the "how
does data move through the system" question twice under two different
topic labels.

## 18.4 Phase-Ordered Ranking (FR15)

The actual fix for "architecture questions have to finish before
implementation questions start" lives entirely in
`orchestrator.py::_rank_pending_items`, not in the planner. The planner
controls *how many* architecture items exist; the ranking function
controls *when* they're asked, and today's ranking has no way to keep a
whole category's items grouped together — see §18.6 for why a
plan-order-only fix doesn't work.

```python
def _phase(category: QuestionCategory) -> int:
    return 0 if category == "architecture" else 1

def _priority(item: QARecordRow) -> tuple[int, bool, bool]:
    target = item.target_file or item.target_module
    is_duplicate_target = bool(target) and target in already_asked_targets
    is_repeat_category = item.category in already_asked_categories
    return (_phase(item.category), is_duplicate_target, is_repeat_category)
```

`phase` is an `int` (0/1), not folded into the existing booleans, so it
sorts first without relying on Python's boolean-is-an-int coercion
reading as intentional. Everything else — duplicate-target and
repeat-category as secondary/tertiary keys, `sorted()`'s stability for
final tie-breaking — is untouched. This also means phase ordering
**re-derives itself from category on every call**, not just once at
session start — a consequence spelled out in §18.5, since it's what
makes replenishment reopen phase 0 automatically with zero special
casing.

## 18.5 Dynamic Budget and Replenishment (FR29)

**Sizing.** `Config.max_questions`'s default changes from a flat `"8"` to
computed from duration: `max(1, viva_duration_minutes // 2)`, roughly one
question per two minutes of session time. An explicit `MAX_QUESTIONS` env
var still wins — this is a default change, not a removal of the
override, so nobody pinning it today sees a behavior change.

**Why sizing alone isn't enough.** The live loop
(`orchestrator.py::_run_live_session`) already has two distinct exit
paths: `TIME_EXPIRED` when the timer runs out, and `QUESTIONS_EXHAUSTED`
when `get_pending_plan_items()` comes back empty — which happens once the
initial plan plus follow-ups (capped by `MAX_FOLLOWUP_DEPTH`, default 1)
are used up. A budget matched to an *average* 2-minutes-per-answer pace
means anyone answering faster than average empties the plan before the
clock, and duration-derived sizing makes this more visible than the old
flat default of 8 did (the old default over-provisioned by accident).

**Replenishment, not just sizing.** When `pending` is empty and the timer
hasn't expired, the Orchestrator calls `build_coverage_plan()` again with
a higher `max_questions` ceiling instead of ending the session.
`build_coverage_plan()` is deterministic given `(profile, max_questions)`
— Pass 2's module round-robin and Pass 3's file-level fallback already
know how to keep manufacturing more specific coverage rather than leaving
slots idle, they just stop at a fixed ceiling today. Raising the ceiling
and re-running produces a superset of the original plan; the whole
rebuilt plan can simply be re-saved through `save_plan()`, whose
`INSERT OR IGNORE` (keyed on `(session_id, question_id)`, already in
place for the follow-up-item case) silently skips anything whose
`question_id` already exists — this turned out to already solve the
manual-ID-diffing problem this design doc originally assumed would be
needed (see §18.6).

Because phase is re-derived from category on every `_rank_pending_items`
call (§18.4), a replenishment that adds fresh `architecture` items
reopens phase 0 automatically — a fast answerer who blows through the
whole plan can see a new batch of architecture-tier questions appear
after implementation-tier questions already started. This is intentional
(confirmed during design review): phase ordering is a live property of
what's currently pending, not a one-time decision baked in at session
start.

**Termination guarantee.** Replenishment must still terminate.
`QUESTIONS_EXHAUSTED` becomes the terminal state only when a
replenishment call also returns zero *new* items beyond what's already
asked/pending — at that point the repo has genuinely run out of
groundable modules/files/topics, and the loop must not retry the same or
higher ceiling again in that session (see §18.6 for the busy-loop this
prevents).

## 18.6 Issues Found in Design Review (before implementation)

Recorded here per the project convention of putting root causes in
design docs, not just silently fixing them — these were caught in
panel review, not via a real-session bug report, so there's no §X.9
production-bug writeup to point to, but the same discipline applies:
document why, not just what.

- **Dedup-key collision (§18.2).** `_add()`'s key
  `(category, target_module, target_file)` treats every architecture
  item as identical (`target_module`/`target_file` are both `None` for
  all of them), so a second architecture item is silently dropped as a
  duplicate before this fix. Caught by reasoning through Pass 1's loop
  against the new multi-topic requirement, not by running it — would
  have been a very confusing bug to find later, since `build_coverage_plan()`
  would return fewer items than requested with no error.
- **Plan-order-only ordering doesn't work.** An earlier version of this
  design considered just emitting all architecture items first in
  `build_coverage_plan()`'s output list and relying on that order. It
  doesn't work: `_rank_pending_items`'s existing tie-break is
  "not-yet-asked category beats already-asked category", so the moment
  the first architecture question is asked, `architecture` becomes an
  already-asked category and every *other* never-touched category (all
  still reading `(False, False)`) jumps ahead of the second architecture
  item. The fix has to be the `phase` key in the ranking function itself
  (§18.4), not the plan's insertion order.
- **Replenishment ID collision -- resolved for free.** The original
  design worry: `build_coverage_plan()` assigns IDs sequentially from
  `q_01` on every call, so calling it again with a higher ceiling and
  inserting the *whole* returned list would re-generate the same IDs for
  the first N items, risking a collision against rows already in
  `SessionStore`. Turned out `save_plan()` already uses `INSERT OR
  IGNORE` keyed on `(session_id, question_id)` (originally added for the
  follow-up-item case), so re-saving the entire rebuilt plan is already
  safe -- no manual diffing needed. Worth recording as a "checked, not
  just assumed" note rather than silently dropping the concern.
- **Replenishment busy-loop.** A repo that's already fully covered (every
  module, every file, every topic exhausted) would, without a stopping
  rule, get asked again on the very next empty-`pending` check with the
  same or a bumped ceiling, get the same fully-covered plan back, and
  loop. The fix is in §18.5's termination guarantee: a replenishment call
  returning zero new items ends the session for good, no retry at a
  higher ceiling in the same session.
- **Unbounded architecture round-robin would starve everything else.**
  An architecture topic has no natural "exhausted" signal the way a
  `(category, module)` pair does (there's always another question you
  could ask about "pipeline"), so a Pass 1.5 shaped exactly like Pass
  2's round-robin — loop until nothing new can be added — would never
  stop on its own and would consume 100% of any remaining budget before
  Pass 2 got a single slot. Found while implementing, not during panel
  review. Fixed by capping Pass 1.5 at exactly one extra question per
  topic; documented as a future config knob if that turns out too thin.

## 18.7 What This Phase Does Not Change

- `QuestionCategory`'s five fixed values (FR12's category set) —
  untouched.
- `storage/schema.py`'s `category` column and `report.py`'s per-category
  rollup — untouched; every architecture-topic question still reports as
  `category = "architecture"`.
- FR14's follow-up mechanism and `MAX_FOLLOWUP_DEPTH` — untouched;
  follow-ups still queue exactly as before, independent of the
  replenishment path.
- The embedding-similarity duplicate check (`_is_semantic_duplicate`) —
  untouched; it already operates session-wide regardless of category or
  topic.

## 18.8 Real-World Bug: Session Duration Never Reached the Planner

Found via a real timed session against `Dhruv0306/throttle4j`, web UI,
5-minute duration selected. The resulting report showed 9 questions
planned — exactly `_BASELINE_SLOTS` (5 architecture topics + 4 other
categories), with zero room for the Pass 1.5 top-up — regardless of the
5-minute choice.

**Root cause:** `Config.max_questions` is computed once, at
`Config.load()` time, from the *process-wide* `VIVA_DURATION_MINUTES`
environment value. `Orchestrator.start()` already accepted a per-call
`duration_minutes` (both the CLI's `--duration` flag and the web UI's
session-creation request already threaded a per-session choice all the
way to `start()`), but that value was used *only* to compute
`duration_seconds` for the live-session timer. `_run_planning()` called
`build_coverage_plan(profile, self.config)` — `self.config` was fixed at
process startup, so it had no way to know this particular session had
asked for something shorter (or longer) than the `.env` default. §18.5's
"budget scales with duration" fix scaled the wrong duration: the
environment-level default, not the session-level actual.

**Fix:** `Config` gains `max_questions_explicit: bool`, set at load time
from whether `MAX_QUESTIONS` was present in the environment at all — this
is the piece that was missing before, since a plain `int` value can't
distinguish "the operator pinned this" from "this was derived and could
legitimately be recomputed for a different duration." `start()` computes
`effective_duration_minutes` (the caller's explicit choice, or
`config.viva_duration_minutes` as before) and passes it to
`_run_planning()`, which builds a per-session planning `Config` via
`dataclasses.replace()` — recomputing `max_questions` from *this
session's* duration — whenever `max_questions_explicit` is `False`. An
explicit `MAX_QUESTIONS` pin is left untouched regardless of what any
individual session asks for, matching the existing "explicit always
wins" rule from §18.5.

This is the same discipline as any other bug found via real-world
testing (docs/system-design/10-phase-5-questiongen-design.md §10.8,
§10.9, §10.10; docs/system-design/11-phase-6-session-loop-design.md
§11.9) — documented here rather than fixed silently, even though it
surfaced after this phase's patches had already been delivered as a
series. Per the project's standing rule, this is landed as a new patch
on top of the already-delivered series, not a rebase of any prior
commit.
