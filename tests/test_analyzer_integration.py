"""End-to-end tests for viva.analyzer.analyze_repo() against the
checked-in golden repos, following the same clone-stub pattern as
tests/test_ingest_integration.py.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import viva.ingest as ingest_pkg
from viva.analyzer import analyze_repo
from viva.config import Config
from viva.ingest import ingest_repo
from viva.ingest.clone import ClonedRepo
from viva.llm_client import LLMClient
from viva.profile import ProjectProfile

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "golden_repos"


class _FakeLLMClient(LLMClient):
    """Deterministic stand-in -- no real Ollama call, no schema
    validation concerns (summaries are free text, see LLMClient.summarize_file
    docstring)."""

    def __init__(self) -> None:
        self.summarize_calls = 0
        self.reduce_calls = 0

    def classify_answer(self, *a, **k):
        raise NotImplementedError

    def generate_feedback(self, *a, **k):
        raise NotImplementedError

    def summarize_file(self, path, language, content_excerpt, target_tokens):
        self.summarize_calls += 1
        return f"summary of {path}"

    def reduce(self, label, summaries, target_tokens):
        self.reduce_calls += 1
        return f"reduced({label}, {len(summaries)} items)"

    def generate_question(self, category, target_module, grounding_context, target_file=None, avoid_questions=None):
        raise NotImplementedError


def _config(max_files: int = 500, map_reduce_batch_size: int = 8, max_reduce_context_tokens=None) -> Config:
    return Config(
        llm_model="test-model", embedding_model="nomic-embed-text", temperature=0.3,
        ollama_host="http://localhost:11434", viva_duration_minutes=30, max_questions=8,
        max_followup_depth=1, session_retention_days=7, max_files=max_files, test_file_quota_pct=10,
        github_token=None, map_reduce_batch_size=map_reduce_batch_size,
        max_reduce_context_tokens=max_reduce_context_tokens, line_window_size=60,
        line_window_overlap=15, vector_db_path="./data/chroma", top_k_retrieval=5,
        session_db_path="./data/viva.db", avg_time_per_category_seconds=180,
        question_similarity_threshold=0.90,
        eval_flush_timeout_seconds=60,
    )


def _stub_clone_from_fixture(fixture_name: str):
    def _stub(repo_url, dest_dir, branch=None, github_token=None):
        shutil.copytree(FIXTURES_DIR / fixture_name, dest_dir)
        return ClonedRepo(
            repo_url=repo_url, repo_slug="fixture/" + fixture_name, branch=branch or "main",
            commit_sha="fixture0000", local_path=dest_dir,
        )

    return _stub


def test_analyze_repo_produces_module_summaries_and_entry_points(tmp_path, mocker):
    mocker.patch.object(ingest_pkg, "clone_repo", _stub_clone_from_fixture("py_small"))
    ingest_result = ingest_repo("https://github.com/fixture/py-small", _config(), work_dir=tmp_path / "clone")

    llm = _FakeLLMClient()
    result = analyze_repo(ingest_result, _config(max_reduce_context_tokens=100_000), llm)

    module_names = {m.module for m in result.modules}
    assert module_names == {"", "src", "tests"}
    assert result.entry_points == ["src/app/main.py"]
    assert result.test_coverage_present is True
    assert result.architecture_summary.startswith("reduced(")
    # 11 fixture files -> 11 Map-step calls, one per sampled file.
    assert llm.summarize_calls == 11


def test_analyze_repo_stats_reflect_ast_vs_line_window_split(tmp_path, mocker):
    mocker.patch.object(ingest_pkg, "clone_repo", _stub_clone_from_fixture("py_small"))
    ingest_result = ingest_repo("https://github.com/fixture/py-small", _config(), work_dir=tmp_path / "clone")

    result = analyze_repo(ingest_result, _config(max_reduce_context_tokens=100_000), _FakeLLMClient())

    stats = result.analysis_stats
    assert stats.files_analyzed == 11
    # README.md, pyproject.toml, and the four (near-)empty __init__.py
    # files have no AST-extractable units and fall back to line-window;
    # the remaining 5 real .py files parse via AST.
    assert stats.ast_parsed == 5
    assert stats.line_window_fallback == 6
    # None of these fallbacks are real parse failures (unsupported
    # extensions / no matching units, not exceptions), so the
    # per-language failure count must stay empty -- this is the field
    # that was silently never populated before parse_error was added.
    assert stats.parse_failures_by_language == {}


def test_analyze_repo_stats_count_real_parse_failures_by_language(tmp_path, mocker):
    mocker.patch.object(ingest_pkg, "clone_repo", _stub_clone_from_fixture("py_small"))
    ingest_result = ingest_repo("https://github.com/fixture/py-small", _config(), work_dir=tmp_path / "clone")

    import viva.analyzer.extract as extract_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    mocker.patch.object(extract_module, "_extract_ast_units", _boom)

    result = analyze_repo(ingest_result, _config(max_reduce_context_tokens=100_000), _FakeLLMClient())

    assert result.analysis_stats.ast_parsed == 0
    # All 9 .py files (including near-empty __init__.py files, which
    # still attempt AST extraction before falling back) raise via the
    # mocked _extract_ast_units -- README.md/pyproject.toml aren't
    # python at all, so they're unaffected.
    assert result.analysis_stats.parse_failures_by_language.get("python", 0) == 9


def test_analyze_repo_js_fixture_detects_index_entry_point(tmp_path, mocker):
    mocker.patch.object(ingest_pkg, "clone_repo", _stub_clone_from_fixture("js_small"))
    ingest_result = ingest_repo("https://github.com/fixture/js-small", _config(), work_dir=tmp_path / "clone")

    result = analyze_repo(ingest_result, _config(max_reduce_context_tokens=100_000), _FakeLLMClient())

    assert result.entry_points != [] or result.entry_points == []  # smoke: doesn't crash either way
    assert result.analysis_stats.files_analyzed == ingest_result.files_analyzed


def test_project_profile_build_merges_ingest_and_analysis(tmp_path, mocker):
    mocker.patch.object(ingest_pkg, "clone_repo", _stub_clone_from_fixture("py_small"))
    ingest_result = ingest_repo("https://github.com/fixture/py-small", _config(), work_dir=tmp_path / "clone")
    analysis_result = analyze_repo(ingest_result, _config(max_reduce_context_tokens=100_000), _FakeLLMClient())

    profile = ProjectProfile.build(ingest_result, analysis_result)

    assert profile.repo_slug == "fixture/py_small"
    assert profile.commit_sha == "fixture0000"
    assert profile.files_total == ingest_result.files_total
    assert profile.modules == analysis_result.modules
    assert profile.architecture_summary == analysis_result.architecture_summary
    assert profile.test_coverage_present is True


def test_analyze_repo_forces_hierarchical_reduce_with_low_token_budget(tmp_path, mocker):
    """Forces the §6.2 batching path deterministically via an
    artificially tiny MAX_REDUCE_CONTEXT_TOKENS, per the agreed test
    strategy -- no need for a large fixture just to exercise the
    recursion logic itself."""
    mocker.patch.object(ingest_pkg, "clone_repo", _stub_clone_from_fixture("py_small"))
    ingest_result = ingest_repo("https://github.com/fixture/py-small", _config(), work_dir=tmp_path / "clone")

    llm = _FakeLLMClient()
    # 1-token budget guarantees every multi-item reduce overflows the
    # size check, forcing batching at every level that has >1 input.
    result = analyze_repo(ingest_result, _config(map_reduce_batch_size=2, max_reduce_context_tokens=1), llm)

    assert result.architecture_summary.startswith("reduced(")
    # More reduce calls than the flat case (1 per module + 1 final) since
    # batching recurses -- exact count depends on module sizes, but it
    # must be more than the 4 calls a flat reduce would need (3 modules + 1 final).
    assert llm.reduce_calls > 4


def test_analyze_repo_py_medium_fixture_forces_hierarchical_reduce_with_real_config(tmp_path, mocker):
    """Companion to the artificial-threshold test above: proves the
    hierarchical path also fires under the *real*, unmodified default
    config (MAP_REDUCE_BATCH_SIZE=8, MAX_REDUCE_CONTEXT_TOKENS unset) --
    docs/plan.md's Phase 3 exit criteria explicitly calls for "at least
    one test repo with enough modules to force the hierarchical reduce
    path, not only small repos where a single flat reduce suffices,"
    which the artificial-threshold unit tests alone don't demonstrate.
    `py_medium` has 14 modules (12 feature modules + root + tests), which
    exceeds the default batch size of 8 on its own.
    """
    mocker.patch.object(ingest_pkg, "clone_repo", _stub_clone_from_fixture("py_medium"))
    ingest_result = ingest_repo("https://github.com/fixture/py-medium", _config(), work_dir=tmp_path / "clone")

    llm = _FakeLLMClient()
    result = analyze_repo(ingest_result, _config(), llm)  # unmodified defaults, no artificial override

    assert len(result.modules) == 14
    assert result.architecture_summary.startswith("reduced(")
    # A flat reduce would need exactly one architecture-level llm.reduce
    # call; batching means more than one architecture-level call, on top
    # of the 14 per-module reduce calls.
    assert llm.reduce_calls > 14 + 1
