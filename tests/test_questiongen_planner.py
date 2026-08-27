from __future__ import annotations

from viva.analyzer.models import AnalysisStats
from viva.config import Config
from viva.ingest.models import ExclusionStats
from viva.profile import ProjectProfile
from viva.questiongen.planner import build_coverage_plan

_ALL_CATEGORIES = {
    "architecture", "implementation_detail", "tech_choice_rationale",
    "error_handling", "testing_strategy",
}


def _config(max_questions: int = 8) -> Config:
    return Config(
        llm_model="test-model", embedding_model="nomic-embed-text", temperature=0.3,
        ollama_host="http://localhost:11434", viva_duration_minutes=30, max_questions=max_questions,
        max_followup_depth=1, session_retention_days=7, max_files=500, test_file_quota_pct=10,
        github_token=None, map_reduce_batch_size=8, max_reduce_context_tokens=100_000,
        line_window_size=60, line_window_overlap=15, vector_db_path="./data/chroma", top_k_retrieval=5,
    )


class _Module:
    def __init__(self, module: str, file_count: int):
        self.module = module
        self.file_count = file_count
        self.summary = f"summary of {module}"


def _profile(modules) -> ProjectProfile:
    return ProjectProfile(
        repo_url="https://github.com/o/r", repo_slug="o/r", commit_sha="sha", branch="main",
        local_path="/tmp/x", files_total=10, files_analyzed=10, sampled_files=[], excluded_notable=[],
        sampling_note="", detected_stack=["python"], exclusion_stats=ExclusionStats(),
        architecture_summary="arch", modules=modules, entry_points=["main.py"],
        test_coverage_present=True, analysis_stats=AnalysisStats(),
    )


def test_plan_covers_every_category_at_least_once():
    profile = _profile([_Module("auth", 5), _Module("payments", 3)])
    plan = build_coverage_plan(profile, _config(max_questions=5))

    categories_seen = {item.category for item in plan}
    assert categories_seen == _ALL_CATEGORIES


def test_architecture_item_has_no_target_module():
    profile = _profile([_Module("auth", 5)])
    plan = build_coverage_plan(profile, _config(max_questions=5))

    arch_items = [i for i in plan if i.category == "architecture"]
    assert len(arch_items) == 1
    assert arch_items[0].target_module is None


def test_plan_never_exceeds_max_questions():
    profile = _profile([_Module("auth", 5), _Module("payments", 3), _Module("api", 2)])
    plan = build_coverage_plan(profile, _config(max_questions=6))

    assert len(plan) <= 6


def test_plan_never_duplicates_category_module_pairs():
    profile = _profile([_Module("auth", 5), _Module("payments", 3)])
    plan = build_coverage_plan(profile, _config(max_questions=8))

    keys = [(item.category, item.target_module) for item in plan]
    assert len(keys) == len(set(keys))


def test_bigger_modules_get_more_coverage_when_slots_remain():
    profile = _profile([_Module("auth", 50), _Module("tiny", 1)])
    plan = build_coverage_plan(profile, _config(max_questions=8))

    auth_count = sum(1 for i in plan if i.target_module == "auth")
    tiny_count = sum(1 for i in plan if i.target_module == "tiny")
    assert auth_count >= tiny_count


def test_plan_shorter_than_max_questions_for_small_repo():
    # One module means every module-scoped category is exhausted after
    # one round -- the plan must not pad with duplicate pairs to reach
    # max_questions.
    profile = _profile([_Module("only", 3)])
    plan = build_coverage_plan(profile, _config(max_questions=20))

    assert len(plan) == len(_ALL_CATEGORIES)


def test_plan_with_no_modules_grounds_every_category_project_level():
    # No modules to scope module-specific categories to -- every category
    # falls back to target_module=None (project-level context) rather
    # than the planner failing or fabricating a module name.
    profile = _profile([])
    plan = build_coverage_plan(profile, _config(max_questions=8))

    assert len(plan) == len(_ALL_CATEGORIES)
    assert all(item.target_module is None for item in plan)
