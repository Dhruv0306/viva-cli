from __future__ import annotations

from viva.analyzer.models import AnalysisStats
from viva.config import Config
from viva.ingest.models import ExclusionStats, SampledFile
from viva.profile import ProjectProfile
from viva.questiongen.planner import build_coverage_plan
from viva.questiongen.retrieval import ARCHITECTURE_TOPICS

_ALL_CATEGORIES = {
    "architecture", "implementation_detail", "tech_choice_rationale",
    "error_handling", "testing_strategy",
}

# Phase 13 baseline: one slot per architecture topic + one slot per each
# of the other four categories (Pass 1), then one extra slot per
# architecture topic (Pass 1.5) before Pass 2/3 get anything. Computed
# from the real registry rather than hardcoded, so these tests don't
# silently rot if a topic is added or removed later.
_ARCH_TOPIC_COUNT = len(ARCHITECTURE_TOPICS)
_BASELINE_SLOTS = _ARCH_TOPIC_COUNT + 4  # topics + the other 4 categories
_WITH_ARCH_TOPUP_SLOTS = _BASELINE_SLOTS + _ARCH_TOPIC_COUNT  # + Pass 1.5


def _config(max_questions: int = 8) -> Config:
    return Config(
        llm_model="test-model", embedding_model="nomic-embed-text", temperature=0.3,
        ollama_host="http://localhost:11434", viva_duration_minutes=30, max_questions=max_questions,
        max_followup_depth=1, session_retention_days=7, max_files=500, test_file_quota_pct=10,
        github_token=None, map_reduce_batch_size=8, max_reduce_context_tokens=100_000,
        line_window_size=60, line_window_overlap=15, vector_db_path="./data/chroma", top_k_retrieval=5,
        session_db_path="./data/viva.db", avg_time_per_category_seconds=180,
        question_similarity_threshold=0.90,
        eval_flush_timeout_seconds=60,
        report_max_items_per_section=10,
        voice_enabled=False, stt_model_size="base", tts_voice="en_US-lessac-medium",
        voice_cache_dir="./data/voice_models", voice_max_answer_seconds=120,
        voice_silence_timeout_seconds=2.5,
    )


class _Module:
    def __init__(self, module: str, file_count: int):
        self.module = module
        self.file_count = file_count
        self.summary = f"summary of {module}"


def _profile(modules, sampled_files=None) -> ProjectProfile:
    return ProjectProfile(
        repo_url="https://github.com/o/r", repo_slug="o/r", commit_sha="sha", branch="main",
        local_path="/tmp/x", files_total=10, files_analyzed=10,
        sampled_files=sampled_files or [], excluded_notable=[],
        sampling_note="", detected_stack=["python"], exclusion_stats=ExclusionStats(),
        architecture_summary="arch", modules=modules, entry_points=["main.py"],
        test_coverage_present=True, analysis_stats=AnalysisStats(),
    )


def test_plan_covers_every_category_at_least_once():
    # Needs enough budget to clear the architecture-topic baseline (Pass
    # 1's per-topic slots) before the other four categories get theirs.
    profile = _profile([_Module("auth", 5), _Module("payments", 3)])
    plan = build_coverage_plan(profile, _config(max_questions=_BASELINE_SLOTS))

    categories_seen = {item.category for item in plan}
    assert categories_seen == _ALL_CATEGORIES


def test_small_budget_prioritizes_architecture_topics_over_other_categories():
    # With a budget smaller than the architecture-topic baseline, Pass 1
    # spends every slot on architecture topics first -- the other four
    # categories get nothing yet. This is the ordering-at-the-plan-level
    # half of "architecture clears before implementation"; the other
    # half (runtime ask order even when both are already planned) is
    # orchestrator.py's phase-keyed ranking, not this function.
    profile = _profile([_Module("auth", 5)])
    plan = build_coverage_plan(profile, _config(max_questions=min(3, _ARCH_TOPIC_COUNT)))

    assert all(item.category == "architecture" for item in plan)


def test_architecture_gets_one_item_per_topic():
    profile = _profile([_Module("auth", 5)])
    plan = build_coverage_plan(profile, _config(max_questions=_BASELINE_SLOTS))

    arch_items = [i for i in plan if i.category == "architecture"]
    assert len(arch_items) == _ARCH_TOPIC_COUNT
    assert {i.architecture_topic for i in arch_items} == set(ARCHITECTURE_TOPICS)
    assert all(i.target_module is None for i in arch_items)


def test_architecture_topics_get_a_second_question_when_budget_allows():
    # Pass 1.5: one extra question per topic before Pass 2 starts
    # spending budget on per-module categories.
    profile = _profile([_Module("auth", 5)])
    plan = build_coverage_plan(profile, _config(max_questions=_WITH_ARCH_TOPUP_SLOTS))

    arch_items = [i for i in plan if i.category == "architecture"]
    assert len(arch_items) == _ARCH_TOPIC_COUNT * 2
    per_topic_counts = {topic: 0 for topic in ARCHITECTURE_TOPICS}
    for item in arch_items:
        per_topic_counts[item.architecture_topic] += 1
    assert all(count == 2 for count in per_topic_counts.values())


def test_architecture_extra_round_is_capped_at_one_not_unbounded():
    # Pass 1.5 gives one extra question per topic, then stops -- it must
    # not consume every remaining slot even when budget is generous,
    # since that would starve Pass 2/3 of the module-scoped categories
    # entirely.
    profile = _profile([_Module("auth", 5), _Module("payments", 3)])
    plan = build_coverage_plan(profile, _config(max_questions=_WITH_ARCH_TOPUP_SLOTS + 4))

    arch_items = [i for i in plan if i.category == "architecture"]
    assert len(arch_items) == _ARCH_TOPIC_COUNT * 2
    assert len(plan) > len(arch_items)  # Pass 2 got some of the remaining budget


def test_plan_never_exceeds_max_questions():
    profile = _profile([_Module("auth", 5), _Module("payments", 3), _Module("api", 2)])
    plan = build_coverage_plan(profile, _config(max_questions=_BASELINE_SLOTS + 2))

    assert len(plan) <= _BASELINE_SLOTS + 2


def test_plan_never_duplicates_category_module_pairs():
    # Non-architecture categories still can't duplicate a (category,
    # module) pair -- architecture is deliberately exempt (see
    # test_architecture_topics_get_a_second_question_when_budget_allows),
    # so it's excluded from this check.
    profile = _profile([_Module("auth", 5), _Module("payments", 3)])
    plan = build_coverage_plan(profile, _config(max_questions=_WITH_ARCH_TOPUP_SLOTS + 6))

    keys = [
        (item.category, item.target_module)
        for item in plan
        if item.category != "architecture"
    ]
    assert len(keys) == len(set(keys))


def test_bigger_modules_get_more_coverage_when_slots_remain():
    profile = _profile([_Module("auth", 50), _Module("tiny", 1)])
    plan = build_coverage_plan(profile, _config(max_questions=_WITH_ARCH_TOPUP_SLOTS + 4))

    auth_count = sum(1 for i in plan if i.target_module == "auth")
    tiny_count = sum(1 for i in plan if i.target_module == "tiny")
    assert auth_count >= tiny_count


def test_plan_shorter_than_max_questions_for_small_repo():
    # One module means every module-scoped category is exhausted after
    # one round -- the plan must not pad with duplicate pairs to reach
    # max_questions. Total is the architecture baseline+topup (fixed,
    # topic-count-driven) plus one module-level item per per-module
    # category (implementation_detail/tech_choice_rationale/
    # error_handling already got their Pass-1 slot; testing_strategy
    # also already got its Pass-1 slot) -- i.e. exactly the baseline +
    # topup, nothing more, since there's no second module to round-robin
    # onto and Pass 3 has no extra files to fall back to either.
    profile = _profile([_Module("only", 3)])
    plan = build_coverage_plan(profile, _config(max_questions=200))

    assert len(plan) == _WITH_ARCH_TOPUP_SLOTS


def test_plan_with_no_modules_grounds_every_category_project_level():
    # No modules to scope module-specific categories to -- every category
    # falls back to target_module=None (project-level context) rather
    # than the planner failing or fabricating a module name.
    profile = _profile([])
    plan = build_coverage_plan(profile, _config(max_questions=200))

    assert len(plan) == _WITH_ARCH_TOPUP_SLOTS
    assert all(item.target_module is None for item in plan)


# --- Regression tests for the click real-repo run ---
#
# `tests/`/`docs/` outsizing the actual source directory (click: many
# doc pages, a large test suite, a comparatively small `click/` package)
# caused every module-scoped category to plan against `tests` or `docs`
# instead of `click` -- see docs/system-design/10-phase-5-questiongen-design.md
# §10.8 for the full root-cause trace.

def test_non_source_modules_excluded_from_implementation_style_categories():
    # "tests" and "docs" both outsize the real source module "click", as
    # observed against the real pallets/click repo -- they must never be
    # picked as the target for implementation_detail/tech_choice_rationale/
    # error_handling.
    profile = _profile([
        _Module("tests", 150), _Module("docs", 60), _Module("click", 25),
    ])
    plan = build_coverage_plan(profile, _config(max_questions=_WITH_ARCH_TOPUP_SLOTS + 6))

    for item in plan:
        if item.category in ("implementation_detail", "tech_choice_rationale", "error_handling"):
            assert item.target_module == "click", item


def test_testing_strategy_targets_dedicated_test_module_even_when_not_largest():
    # "click" (the source module) is the largest by file_count, but
    # testing_strategy should still target "tests" specifically -- a
    # where={"module": "click"} filter would never surface test chunks
    # for a repo that keeps all its tests under a separate top-level dir.
    profile = _profile([
        _Module("click", 100), _Module("tests", 60), _Module("docs", 20),
    ])
    plan = build_coverage_plan(profile, _config(max_questions=_WITH_ARCH_TOPUP_SLOTS + 6))

    testing_items = [i for i in plan if i.category == "testing_strategy"]
    assert len(testing_items) == 1
    assert testing_items[0].target_module == "tests"


def test_testing_strategy_falls_back_to_source_module_without_dedicated_test_dir():
    # No tests/-shaped directory at all (e.g. Go-style co-located
    # *_test.go files) -- fall back to the largest source module rather
    # than leaving testing_strategy ungrounded, relying on retrieval.py's
    # test-path preference to surface co-located test chunks within it.
    profile = _profile([_Module("app", 30), _Module("docs", 10)])
    plan = build_coverage_plan(profile, _config(max_questions=_WITH_ARCH_TOPUP_SLOTS + 6))

    testing_items = [i for i in plan if i.category == "testing_strategy"]
    assert testing_items[0].target_module == "app"


def test_all_non_source_modules_falls_back_to_unfiltered_pool():
    # A repo with no identifiable source directory at all (e.g. root
    # files only, everything else under docs/examples) shouldn't leave
    # implementation-style categories with no target -- degrade to the
    # unfiltered module pool rather than producing an empty plan.
    profile = _profile([_Module("docs", 20), _Module("examples", 10)])
    plan = build_coverage_plan(profile, _config(max_questions=_WITH_ARCH_TOPUP_SLOTS + 6))

    impl_items = [i for i in plan if i.category == "implementation_detail"]
    assert len(impl_items) >= 1
    assert all(i.target_module in ("docs", "examples") for i in impl_items)


def test_dot_prefixed_directories_excluded_even_when_not_denylisted():
    # Regression test: found via a real `viva questiongen` run against
    # pallets/click, which uses a src/-layout (top-level dirs: src,
    # tests, docs, examples, .github). The hardcoded denylist correctly
    # excluded docs/examples but had never anticipated ".github",
    # producing questions grounded in CI workflow YAML instead of code.
    # ".github" itself isn't special-cased -- the dot-prefix rule
    # generalizes to any tooling-config directory.
    profile = _profile([
        _Module("src", 50), _Module("tests", 40), _Module("docs", 20),
        _Module("examples", 8), _Module(".github", 6),
    ])
    plan = build_coverage_plan(profile, _config(max_questions=_WITH_ARCH_TOPUP_SLOTS + 6))

    for item in plan:
        if item.category in ("implementation_detail", "tech_choice_rationale", "error_handling"):
            assert item.target_module == "src", item
        assert item.target_module != ".github"


def test_vendored_and_build_directories_excluded():
    profile = _profile([
        _Module("node_modules", 500), _Module("dist", 30), _Module("app", 15),
    ])
    plan = build_coverage_plan(profile, _config(max_questions=_WITH_ARCH_TOPUP_SLOTS + 6))

    for item in plan:
        if item.category in ("implementation_detail", "tech_choice_rationale", "error_handling"):
            assert item.target_module == "app", item


# --- Pass 3: file-level fallback (§10.10) ---
#
# Found via the same real click run: click only has two real source-ish
# top-level directories (src, tests) once non-source ones are excluded,
# so Pass 2 had nowhere left to distribute extra slots to -- the plan
# stopped short of MAX_QUESTIONS. Pass 3 fills remaining slots with
# file-level items instead of leaving them unused.

def _file(path: str, module: str, size_bytes: int = 1000, always_include: bool = False, is_test: bool = False) -> SampledFile:
    return SampledFile(path=path, size_bytes=size_bytes, module=module, always_include=always_include, is_test=is_test)


def test_pass_3_fills_remaining_slots_with_file_level_items_when_modules_run_out():
    # Single source module (click-shaped: only "src" survives filtering)
    # -- Pass 2 has nothing to distribute to, Pass 3 must pick up the slack.
    files = [
        _file("src/core.py", "src", size_bytes=5000),
        _file("src/parser.py", "src", size_bytes=3000),
        _file("src/utils.py", "src", size_bytes=1000),
    ]
    profile = _profile([_Module("src", 10), _Module("docs", 8)], sampled_files=files)
    budget = _WITH_ARCH_TOPUP_SLOTS + 3
    plan = build_coverage_plan(profile, _config(max_questions=budget))

    assert len(plan) == budget
    file_level_items = [i for i in plan if i.target_file is not None]
    assert len(file_level_items) == 3  # all three files get used, one per per-module category
    assert all(i.target_module == "src" for i in file_level_items)
    assert all(i.target_file in {"src/core.py", "src/parser.py", "src/utils.py"} for i in file_level_items)


def test_pass_3_prefers_largest_files_first():
    files = [
        _file("src/small.py", "src", size_bytes=100),
        _file("src/big.py", "src", size_bytes=9000),
        _file("src/medium.py", "src", size_bytes=500),
    ]
    profile = _profile([_Module("src", 10)], sampled_files=files)
    # Exactly one file-level slot: architecture baseline+topup + the
    # single-module Pass-1 baseline for the three per-module categories,
    # plus one more for Pass 3 to fill with the largest file.
    plan = build_coverage_plan(profile, _config(max_questions=_WITH_ARCH_TOPUP_SLOTS + 1))

    file_level_items = [i for i in plan if i.target_file is not None]
    assert len(file_level_items) == 1
    assert file_level_items[0].target_file == "src/big.py"


def test_pass_3_prefers_always_include_files_over_size():
    files = [
        _file("src/huge_random_file.py", "src", size_bytes=9000, always_include=False),
        _file("src/main.py", "src", size_bytes=200, always_include=True),
    ]
    profile = _profile([_Module("src", 10)], sampled_files=files)
    plan = build_coverage_plan(profile, _config(max_questions=_WITH_ARCH_TOPUP_SLOTS + 1))

    file_level_items = [i for i in plan if i.target_file is not None]
    assert file_level_items[0].target_file == "src/main.py"


def test_pass_3_excludes_test_files():
    files = [
        _file("src/core.py", "src", size_bytes=1000, is_test=False),
        _file("src/test_helpers.py", "src", size_bytes=9000, is_test=True),
    ]
    profile = _profile([_Module("src", 10)], sampled_files=files)
    plan = build_coverage_plan(profile, _config(max_questions=_WITH_ARCH_TOPUP_SLOTS + 1))

    file_level_items = [i for i in plan if i.target_file is not None]
    assert all(i.target_file != "src/test_helpers.py" for i in file_level_items)


def test_pass_3_never_exceeds_max_questions_even_with_many_files():
    files = [_file(f"src/file_{i}.py", "src", size_bytes=1000 - i) for i in range(20)]
    profile = _profile([_Module("src", 20)], sampled_files=files)
    budget = _WITH_ARCH_TOPUP_SLOTS + 4
    plan = build_coverage_plan(profile, _config(max_questions=budget))

    assert len(plan) == budget


def test_pass_3_never_duplicates_category_module_file_triples():
    files = [_file(f"src/file_{i}.py", "src", size_bytes=1000 - i) for i in range(5)]
    profile = _profile([_Module("src", 5)], sampled_files=files)
    plan = build_coverage_plan(profile, _config(max_questions=_WITH_ARCH_TOPUP_SLOTS + 6))

    keys = [
        (i.category, i.target_module, i.target_file)
        for i in plan
        if i.category != "architecture"
    ]
    assert len(keys) == len(set(keys))


def test_pass_3_does_not_run_when_module_level_slots_already_fill_budget():
    # Enough source modules that Pass 2 alone fills every slot -- Pass 3
    # must never kick in and produce redundant file-level items.
    files = [_file("auth/handler.py", "auth", size_bytes=5000)]
    profile = _profile([_Module("auth", 10), _Module("payments", 8), _Module("api", 6)], sampled_files=files)
    plan = build_coverage_plan(profile, _config(max_questions=_BASELINE_SLOTS))

    assert all(item.target_file is None for item in plan)
