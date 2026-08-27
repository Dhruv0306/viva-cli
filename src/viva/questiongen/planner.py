"""FR12: build a question/coverage plan from the Project Profile.

Distribution strategy (docs/system-design/10-phase-5-questiongen-design.md
§10.2): one slot is guaranteed per category first (covering the fixed
5-category set FR12 requires), then remaining slots up to
`config.max_questions` are distributed round-robin across categories,
preferring modules with the highest `file_count` -- bigger modules get
proportionally more coverage without any module going fully unasked-about
as long as slots remain.

`architecture` is deliberately never paired with a specific module -- it
grounds against project-level context (entry points / architecture
summary), not one module's chunks (see `retrieval.py`).
"""
from __future__ import annotations

from viva.config import Config
from viva.profile import ProjectProfile
from viva.questiongen.models import QuestionCategory, QuestionPlanItem

_CATEGORIES: tuple[QuestionCategory, ...] = (
    "architecture",
    "implementation_detail",
    "tech_choice_rationale",
    "error_handling",
    "testing_strategy",
)

# Categories that make sense grounded in one module's code, in the order
# each additional plan slot should try to grab. "architecture" is
# excluded -- it never takes a target_module (see module docstring).
_MODULE_SCOPED_CATEGORIES: tuple[QuestionCategory, ...] = (
    "implementation_detail",
    "tech_choice_rationale",
    "error_handling",
    "testing_strategy",
)


def build_coverage_plan(profile: ProjectProfile, config: Config) -> list[QuestionPlanItem]:
    """Build the initial (non-followup) coverage plan (FR12).

    Bounded by `config.max_questions`. Returns fewer than
    `max_questions` items only if the profile has too few modules to
    fill every round-robin slot (e.g. a very small repo) -- never pads
    with duplicate (category, module) pairs to hit the count.
    """
    max_questions = config.max_questions
    modules_by_size = sorted(profile.modules, key=lambda m: m.file_count, reverse=True)
    module_names = [m.module for m in modules_by_size]

    items: list[QuestionPlanItem] = []
    seen: set[tuple[QuestionCategory, str | None]] = set()

    def _add(category: QuestionCategory, target_module: str | None) -> bool:
        key = (category, target_module)
        if key in seen:
            return False
        seen.add(key)
        items.append(
            QuestionPlanItem(
                id=f"q_{len(items) + 1:02d}",
                category=category,
                target_module=target_module,
            )
        )
        return True

    # Pass 1: guarantee one slot per category (FR12's coverage requirement),
    # using the largest module for each module-scoped category.
    for category in _CATEGORIES:
        if len(items) >= max_questions:
            break
        target_module = None if category == "architecture" else (module_names[0] if module_names else None)
        _add(category, target_module)

    # Pass 2: distribute remaining slots round-robin across module-scoped
    # categories x modules, biggest modules first, until max_questions is
    # hit or every (category, module) combination has been used.
    module_idx = 1  # module_names[0] was already used in Pass 1 above
    exhausted = False
    while len(items) < max_questions and not exhausted:
        exhausted = True
        for category in _MODULE_SCOPED_CATEGORIES:
            if len(items) >= max_questions:
                break
            if module_idx >= len(module_names):
                continue
            if _add(category, module_names[module_idx]):
                exhausted = False
        module_idx += 1

    return items
