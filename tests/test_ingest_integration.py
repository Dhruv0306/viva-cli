"""End-to-end tests for viva.ingest.ingest_repo() against the checked-in
golden repos (tests/fixtures/golden_repos/), per docs/plan.md's Phase 2
exit criteria: "run against 2-3 real test repos of varying size,
including one that exceeds the 500-file cap."

`clone_repo` is monkeypatched to copy a golden fixture into the
destination directory instead of hitting the network -- this keeps the
default test suite fast, deterministic, and runnable without network
access. A separate, opt-in, network-marked test at the bottom of this
file exercises the real clone path against a small public repo; it's
skipped by default (see its skip condition) and is meant to be run
manually, the same way Phase 1's LLM pressure test was run manually
against real Ollama rather than folded into CI.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import viva.ingest as ingest_pkg
from viva.config import Config
from viva.ingest.clone import ClonedRepo

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "golden_repos"


def _make_config(max_files: int = 500, test_file_quota_pct: int = 10) -> Config:
    return Config(
        llm_model="gemma4:e4b",
        embedding_model="nomic-embed-text",
        temperature=0.3,
        ollama_host="http://localhost:11434",
        viva_duration_minutes=30,
        max_questions=8,
        max_followup_depth=1,
        session_retention_days=7,
        max_files=max_files,
        test_file_quota_pct=test_file_quota_pct,
        github_token=None,
        map_reduce_batch_size=8,
        max_reduce_context_tokens=None,
        line_window_size=60,
        line_window_overlap=15,
        vector_db_path="./data/chroma",
        top_k_retrieval=5,
        session_db_path="./data/viva.db",
        avg_time_per_category_seconds=180,
    )


def _stub_clone_from_fixture(fixture_name: str, extra_files: int = 0):
    """Return a stand-in for `clone_repo` that copies a golden fixture
    into `dest_dir` (and optionally pads it with `extra_files` generated
    filler files, to exercise the MAX_FILES-exceeding sampling path
    without checking hundreds of files into this repo) instead of
    performing a real git clone.
    """

    def _stub(repo_url, dest_dir, branch=None, github_token=None):
        shutil.copytree(FIXTURES_DIR / fixture_name, dest_dir)
        for i in range(extra_files):
            padded = dest_dir / "generated" / f"filler_{i}.py"
            padded.parent.mkdir(parents=True, exist_ok=True)
            padded.write_text(f"# generated filler file {i}\nVALUE = {i}\n")
        return ClonedRepo(
            repo_url=repo_url,
            repo_slug="fixture/" + fixture_name,
            branch=branch or "main",
            commit_sha="fixture0000",
            local_path=dest_dir,
        )

    return _stub


def test_ingest_repo_python_fixture_end_to_end(tmp_path: Path, mocker) -> None:
    mocker.patch.object(ingest_pkg, "clone_repo", _stub_clone_from_fixture("py_small"))

    result = ingest_pkg.ingest_repo(
        "https://github.com/fixture/py-small", _make_config(), work_dir=tmp_path / "clone"
    )

    assert result.repo_slug == "fixture/py_small"
    assert result.commit_sha == "fixture0000"
    assert result.files_total == 11  # matches the checked-in fixture's file count
    assert result.files_analyzed == 11  # well under MAX_FILES, no sampling needed
    assert "no sampling needed" in result.sampling_note
    assert result.detected_stack == ["python"]

    sampled_paths = {f.path for f in result.sampled_files}
    assert "README.md" in sampled_paths
    assert "pyproject.toml" in sampled_paths
    assert "tests/test_main.py" in sampled_paths

    readme = next(f for f in result.sampled_files if f.path == "README.md")
    assert readme.always_include is True
    test_file = next(f for f in result.sampled_files if f.path == "tests/test_main.py")
    assert test_file.is_test is True


def test_ingest_repo_js_fixture_detects_node_stack(tmp_path: Path, mocker) -> None:
    mocker.patch.object(ingest_pkg, "clone_repo", _stub_clone_from_fixture("js_small"))

    result = ingest_pkg.ingest_repo(
        "https://github.com/fixture/js-small", _make_config(), work_dir=tmp_path / "clone"
    )

    assert result.detected_stack == ["node"]
    assert result.files_total == 5


def test_ingest_repo_samples_when_exceeding_max_files_cap(tmp_path: Path, mocker) -> None:
    # py_small has 11 real files; pad with 100 generated filler files and
    # cap max_files well below the total, to exercise the sampling path
    # per the Phase 2 exit criteria ("a repo that exceeds the cap").
    mocker.patch.object(
        ingest_pkg, "clone_repo", _stub_clone_from_fixture("py_small", extra_files=100)
    )

    result = ingest_pkg.ingest_repo(
        "https://github.com/fixture/py-small-padded",
        _make_config(max_files=20),
        work_dir=tmp_path / "clone",
    )

    assert result.files_total == 111
    # Budget (20) + the fixture's 3 always-include files (README.md,
    # pyproject.toml, and src/app/main.py -- "main" is an always-include
    # stem) sit outside the cap entirely, so the total lands at exactly
    # budget + 3, not just <= budget.
    assert result.files_analyzed == 23
    assert result.files_analyzed < result.files_total
    assert result.excluded_notable != []
    assert "prioritized by import centrality" in result.sampling_note

    # The real hand-written source should still be well represented
    # relative to the generated filler, since real files carry actual
    # import-centrality signal and the filler files don't reference
    # anything.
    sampled_paths = {f.path for f in result.sampled_files}
    assert "src/app/main.py" in sampled_paths


def test_ingest_repo_exclusion_stats_surface_in_excluded_notable(tmp_path: Path, mocker) -> None:
    def _stub_with_junk(repo_url, dest_dir, branch=None, github_token=None):
        shutil.copytree(FIXTURES_DIR / "py_small", dest_dir)
        junk = dest_dir / "node_modules" / "leftpad" / "index.js"
        junk.parent.mkdir(parents=True, exist_ok=True)
        junk.write_text("module.exports = 1;\n")
        return ClonedRepo(
            repo_url=repo_url,
            repo_slug="fixture/py_small",
            branch="main",
            commit_sha="fixture0000",
            local_path=dest_dir,
        )

    mocker.patch.object(ingest_pkg, "clone_repo", _stub_with_junk)

    result = ingest_pkg.ingest_repo(
        "https://github.com/fixture/py-small", _make_config(), work_dir=tmp_path / "clone"
    )

    assert any("excluded director" in note for note in result.excluded_notable)
    assert result.exclusion_stats.excluded_dirs == 1


@pytest.mark.skip(
    reason=(
        "Opt-in network test: exercises the real git clone path against a "
        "small public repo. Skipped by default so the suite stays "
        "network-free; remove the skip decorator to run it manually."
    )
)
def test_ingest_repo_real_network_clone(tmp_path: Path) -> None:
    config = _make_config()
    result = ingest_pkg.ingest_repo(
        "https://github.com/octocat/Hello-World", config, work_dir=tmp_path / "clone"
    )

    assert result.repo_slug == "octocat/Hello-World"
    assert len(result.commit_sha) == 12
    assert result.files_total >= 1
