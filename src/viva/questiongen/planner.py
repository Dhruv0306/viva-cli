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

**Source vs. non-source modules** (bug found via real-repo testing against
pallets/click, see docs/system-design/10-phase-5-questiongen-design.md
§10.7): `ProjectProfile.modules[].module` is literally the top-level
directory name (`ingest/sampling.py::_top_level_module`) -- `tests` and
`docs` are exactly as much a "module" as the real source package is. A
naive "biggest module by file_count" pick is wrong for
`implementation_detail`/`tech_choice_rationale`/`error_handling`: on
click, `tests/` and `docs/` both have more files than the `click/`
package itself, so every one of those categories got planned against
`tests` or `docs`, and the `where={"module": ...}` retrieval filter then
locked grounding to that directory before the test-path post-filter in
`retrieval.py` ever got a chance to help.

`_is_source_module()` excludes known non-source top-level directory names
from the pool those three categories pick from -- both a hardcoded
denylist vocabulary (tests/docs/examples/scripts/vendor/build/...) and a
general dot-prefix rule (`.github`, `.circleci`, etc., found via a second
instance of this same bug on the same real click run -- see
docs/system-design/10-phase-5-questiongen-design.md §10.7). `testing_strategy` is the
mirror image: it deliberately targets a test-like directory when one
exists (`_find_test_module()`), rather than being distributed across
source modules in Pass 2 the way the other three are -- pairing
`testing_strategy` with e.g. `target_module="click"` would filter
retrieval to a directory that (for a click-shaped repo, where tests live
under a separate top-level `tests/`) contains no test chunks at all.
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

# Categories distributed across *source* modules in Pass 2. Excludes
# "testing_strategy" (targets a dedicated test-like module instead, see
# module docstring) and "architecture" (never module-scoped).
_PER_MODULE_CATEGORIES: tuple[QuestionCategory, ...] = (
    "implementation_detail",
    "tech_choice_rationale",
    "error_handling",
)

# Mirrors ingest/sampling.py's _TEST_DIR_NAMES plus its
# _LOW_PRIORITY_DIR_NAMES vocabulary (docs/scripts/examples) -- not a
# direct import, since these are private to that module and belong to a
# different component (see retrieval.py's _is_test_path docstring for the
# same rationale). "" covers the loose-root-files bucket
# (`_top_level_module` returns "" for files directly under repo root).
_TEST_MODULE_NAMES = frozenset({"tests", "test", "__tests__", "spec", "specs"})
_NON_SOURCE_MODULE_NAMES = _TEST_MODULE_NAMES | frozenset(
    {"docs", "doc", "documentation", "examples", "example", "scripts", "script",
     "sample", "samples", "demo", "demos", "benchmark", "benchmarks", "",
     # Common vendored/build/dependency directories -- not tooling-config
     # (those are covered by the dot-prefix rule in _is_source_module),
     # but just as clearly never a repo's own implementation.
     "node_modules", "vendor", "dist", "build", "target"}
)


def _is_source_module(module_name: str) -> bool:
    if module_name.startswith("."):
        # Dot-prefixed top-level directories are universally tooling/CI
        # config, never source -- found via the same real-repo click run
        # (a second instance of this bug class): a hardcoded denylist
        # will always miss the next repo's specific tooling directory
        # name, but "starts with a dot" generalizes across essentially
        # every repo (.github, .circleci, .vscode, .git, ...) instead of
        # playing whack-a-mole with one entry per ecosystem.
        return False
    return module_name.lower() not in _NON_SOURCE_MODULE_NAMES


def build_coverage_plan(profile: ProjectProfile, config: Config) -> list[QuestionPlanItem]:
    """Build the initial (non-followup) coverage plan (FR12).

    Bounded by `config.max_questions`. Returns fewer than
    `max_questions` items only if the profile has too few source modules
    to fill every round-robin slot (e.g. a very small repo) -- never
    pads with duplicate (category, module) pairs to hit the count.
    """
    max_questions = config.max_questions
    modules_by_size = sorted(profile.modules, key=lambda m: m.file_count, reverse=True)
    all_module_names = [m.module for m in modules_by_size]

    source_module_names = [n for n in all_module_names if _is_source_module(n)]
    # A repo that's genuinely all-docs/all-tests at the top level (no
    # identifiable source directory) shouldn't leave implementation-ish
    # categories with nothing to target -- fall back to the unfiltered
    # list rather than producing an empty plan for those categories.
    if not source_module_names:
        source_module_names = all_module_names

    test_module = next((n for n in all_module_names if n.lower() in _TEST_MODULE_NAMES), None)
    # No dedicated tests/ directory (e.g. co-located *_test.go files) --
    # fall back to the same largest-source-module target the other
    # categories use, relying on retrieval.py's test-path preference to
    # surface the co-located test chunks within it.
    testing_target = test_module if test_module is not None else (
        source_module_names[0] if source_module_names else None
    )

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

    # Pass 1: guarantee one slot per category (FR12's coverage requirement).
    for category in _CATEGORIES:
        if len(items) >= max_questions:
            break
        if category == "architecture":
            target_module = None
        elif category == "testing_strategy":
            target_module = testing_target
        else:
            target_module = source_module_names[0] if source_module_names else None
        _add(category, target_module)

    # Pass 2: distribute remaining slots round-robin across the
    # per-module categories x source modules, biggest modules first,
    # until max_questions is hit or every (category, module) combination
    # has been used.
    module_idx = 1  # source_module_names[0] was already used in Pass 1 above
    exhausted = False
    while len(items) < max_questions and not exhausted:
        exhausted = True
        for category in _PER_MODULE_CATEGORIES:
            if len(items) >= max_questions:
                break
            if module_idx >= len(source_module_names):
                continue
            if _add(category, source_module_names[module_idx]):
                exhausted = False
        module_idx += 1

    return items
