"""Pure question-ordering and plan-sizing logic, split out of
`Orchestrator` (docs/plan.md Phase 22, docs/system-design/27-phase-22-
container-resumption-and-planning-split-design.md §27.2's finding
19.2.2). No `SessionStore` access here -- every function takes the data
it needs as arguments and returns a value; `Orchestrator` still owns
every store read/write and every other side effect (persisting the
replenished plan, computing the already-asked sets from real qa_records).

Deliberately excludes semantic-duplicate detection (`_cosine_similarity`/
`_is_semantic_duplicate`/`_seed_embedding_cache`) -- a different concern
(embedding-based similarity, not target/category/phase ordering) that
stays in `orchestrator.py`. See §27.2 for the full scoping rationale.
"""
from __future__ import annotations

import dataclasses

from viva.config import Config
from viva.profile import ProjectProfile
from viva.questiongen import build_coverage_plan
from viva.questiongen.models import QuestionPlanItem
from viva.storage import QARecordRow

# Phase 13 (docs/system-design/18-phase-13-architecture-tier-questions-
# design.md §18.5): how much higher to raise the max_questions ceiling on
# each replenishment attempt when the live loop's pending queue runs dry
# before the timer expires. Arbitrary but bounded and re-tried as many
# times as the loop keeps draining -- not meant to be tuned precisely,
# just large enough that a fast answerer doesn't trigger a replenishment
# call on almost every single question.
_REPLENISH_INCREMENT = 6


def rank_pending_items(
    pending: list[QARecordRow],
    already_asked_targets: set[str],
    already_asked_categories: set[str],
) -> list[QARecordRow]:
    """FR15 ("track asked topics/files to avoid duplicate questioning and
    to enforce category coverage across the session") + design.md §7's
    category-breadth preference, both as pure *ordering*, never a
    permanent exclusion.

    Two earlier versions of this function each made a one-time,
    pessimistic decision and permanently dropped whatever didn't fit it
    (first duplicate-target items outright, then non-first-per-category
    items when a time-budget estimate looked too tight compared to the
    *starting* time remaining -- which for any short session fired on the
    very first selection, before anything was even asked, locking in a
    fixed question count regardless of how fast the person actually
    answered or how much real time was left afterward). Both were found
    the same way: a real session against github.com/Dhruv0306/throttle4j
    stopped identically at a fixed question count no matter how much time
    was given -- see docs/system-design/11-phase-6-session-loop-design.md
    §11.9.

    The fix, both times: prefer, never exclude. Pending items are ranked
    -- follow-ups first, then items whose target hasn't been asked about
    yet, then items whose category hasn't been asked about yet -- and the
    caller's own natural loop-exit conditions (timer actually expired, or
    truly no pending items left) are what end the session, not a
    pre-computed worst-case guess made once at the start. Returns the
    *full* ranked list (not just the top pick) so `_run_live_session` can
    try several candidates for the semantic-duplicate check without a
    second query.

    Phase 13 (docs/system-design/18-phase-13-architecture-tier-questions-
    design.md §18.4) adds `phase` as the *primary* sort key: `architecture`
    items are phase 0, everything else is phase 1, so every pending
    architecture question clears before any other category is asked. An
    earlier version of this design tried to get the same effect purely
    from `build_coverage_plan()`'s insertion order, emitting every
    architecture item first -- that doesn't survive this function's own
    duplicate-target/repeat-category tie-break: the moment the first
    architecture question is asked, `architecture` becomes an "already
    asked" category, and every *other* never-touched category (still
    reading (False, False)) jumps ahead of the second architecture item.
    `phase` has to live in the ranking function itself, not the plan's
    build order.

    `phase` is re-derived from `item.category` on every call, not cached
    or decided once -- so a plan replenishment (§18.5) that adds fresh
    architecture items reopens phase 0 automatically, with no special-
    casing needed here. This is intentional: phase ordering is a live
    property of what's currently pending, not a one-time decision baked
    in at session start.

    `already_asked_targets`/`already_asked_categories` are computed by
    the caller from real `qa_records` (Orchestrator._already_asked_
    targets()/_already_asked_categories()) -- this function has no store
    access of its own.
    """
    followups = [p for p in pending if p.is_followup_of is not None]
    non_followups = [p for p in pending if p.is_followup_of is None]

    def _phase(category: str) -> int:
        return 0 if category == "architecture" else 1

    def _priority(item: QARecordRow) -> tuple[int, bool, bool]:
        target = item.target_file or item.target_module
        is_duplicate_target = bool(target) and target in already_asked_targets
        is_repeat_category = item.category in already_asked_categories
        return (_phase(item.category), is_duplicate_target, is_repeat_category)

    return followups + sorted(non_followups, key=_priority)


def compute_replenished_plan(
    profile: ProjectProfile,
    config: Config,
    current_top_level_count: int,
) -> list[QuestionPlanItem] | None:
    """Phase 13 (docs/system-design/18-phase-13-architecture-tier-
    questions-design.md §18.5): called when the live loop's pending queue
    is empty but the timer hasn't expired -- FR29.

    `build_coverage_plan()` is deterministic given `(profile,
    max_questions)`, so raising the ceiling and calling it again
    reproduces every already-persisted item identically, plus a genuinely
    new tail. The caller re-saves the whole rebuilt plan via
    `SessionStore.save_plan()`'s `INSERT OR IGNORE` (keyed on
    `(session_id, question_id)`) -- anything whose `question_id` already
    exists is silently skipped, no manual diffing needed here. (The
    design doc's original write-up assumed a manual ID diff would be
    necessary; `INSERT OR IGNORE` already covered it before that patch
    existed.)

    Returns the new plan if it actually grew past `current_top_level_
    count` (the caller loops back and re-fetches pending). Returns `None`
    if the profile has genuinely run out of groundable modules/files/
    topics -- `build_coverage_plan()` returned no more items than what's
    already persisted even with a higher ceiling -- so the caller can
    treat that as a true terminal `QUESTIONS_EXHAUSTED` rather than
    retrying the same or a higher ceiling again in this session (which
    would otherwise busy-loop on every empty-pending check for the rest
    of the timer).

    `current_top_level_count` is the caller's count of already-persisted
    top-level (non-follow-up) items -- this function has no store access
    of its own to derive it.
    """
    replenished_config = dataclasses.replace(
        config, max_questions=current_top_level_count + _REPLENISH_INCREMENT
    )
    new_plan = build_coverage_plan(profile, replenished_config)
    if len(new_plan) <= current_top_level_count:
        return None
    return new_plan
