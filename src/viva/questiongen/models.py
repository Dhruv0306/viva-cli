"""Data contracts for the QuestionGen component (docs/plan.md Phase 5).

`QuestionPlanItem` is the "Question Plan Item" contract already specified
in docs/design.md §6 -- this module is where it actually gets defined as
code, mirroring how `indexer/models.py` turned FR9's chunk shape into
`Chunk`. `QuestionCategory` is FR12's fixed five-category coverage set.

`GeneratedQuestion` pairs a plan item with the LLM's just-in-time output
(FR13) plus the grounding chunks actually used, so a caller (Phase 6's
Orchestrator) can persist a Q&A Record (docs/design.md §6) without having
to re-derive which chunks backed which question.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

QuestionCategory = Literal[
    "architecture",
    "implementation_detail",
    "tech_choice_rationale",
    "error_handling",
    "testing_strategy",
]

PlanItemStatus = Literal["pending", "generated", "skipped_no_grounding"]


@dataclass(frozen=True)
class QuestionPlanItem:
    """One planned (category, target_module) slot (FR12), before the
    question text itself has been generated.

    `target_module` is `None` for the `architecture` category, which
    grounds against project-level context (entry points, architecture
    summary) rather than one module -- see `planner.py`.

    `is_followup_of` is carried on the contract now so Phase 6's session
    loop (FR14) can construct follow-up `QuestionPlanItem`s using the
    same shape, but the planner in this phase only ever produces
    top-level items (`is_followup_of=None`) -- follow-up generation is a
    live-session concept this phase deliberately doesn't own (see
    `docs/system-design/10-phase-5-questiongen-design.md` §10.3).
    """

    id: str
    category: QuestionCategory
    target_module: str | None
    status: PlanItemStatus = "pending"
    is_followup_of: str | None = None


@dataclass(frozen=True)
class GeneratedQuestion:
    """A `QuestionPlanItem` plus its just-in-time generated question text
    and the grounding chunks it was produced from (FR13).

    `grounding_chunk_ids` mirrors the Question Plan Item contract in
    docs/design.md §6 -- kept here rather than only on the plan item
    because grounding is only known *after* retrieval+generation runs,
    not at plan-build time.
    """

    plan_item: QuestionPlanItem
    question_text: str
    grounding_chunk_ids: list[str] = field(default_factory=list)


@dataclass
class QuestionGenStats:
    """QuestionGen-pipeline bookkeeping, mirroring Phase 3/4's
    `AnalysisStats`/`IndexStats` pattern (NFR8 transparency)."""

    plan_items_built: int = 0
    questions_generated: int = 0
    plan_items_skipped_no_grounding: int = 0
