"""Tests for viva.ingest.sampling (Pass B: capping, ranking, allocation).

Synthetic trees again, for the same reason as test_ingest_import_graph.py:
exact control over file counts per module/tier makes the budget-math
assertions exact rather than approximate.
"""
from __future__ import annotations

from pathlib import Path

from viva.ingest.models import ImportGraph
from viva.ingest.sampling import rank_and_sample


def _write(root: Path, rel_path: str, content: str = "x = 1\n") -> Path:
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def _empty_graph(files: list[Path]) -> ImportGraph:
    return ImportGraph(in_degree={f: 0 for f in files}, edges={})


def test_no_sampling_when_under_budget(tmp_path: Path) -> None:
    files = [
        _write(tmp_path, "src/main.py"),
        _write(tmp_path, "src/utils.py"),
        _write(tmp_path, "README.md"),
    ]

    outcome = rank_and_sample(
        files=files,
        root=tmp_path,
        import_graph=_empty_graph(files),
        max_files=10,
        test_file_quota_pct=10,
    )

    assert len(outcome.sampled) == 3
    assert outcome.excluded_notable == []
    assert "no sampling needed" in outcome.sampling_note


def test_always_include_files_bypass_the_budget(tmp_path: Path) -> None:
    files = [_write(tmp_path, "README.md")]
    files += [_write(tmp_path, f"src/module{i}.py") for i in range(20)]

    outcome = rank_and_sample(
        files=files,
        root=tmp_path,
        import_graph=_empty_graph(files),
        max_files=5,
        test_file_quota_pct=0,
    )

    readme = next(f for f in outcome.sampled if f.path == "README.md")
    assert readme.always_include is True
    # Budget (5) is spent entirely on the 20 candidates; README doesn't
    # eat into it.
    assert len(outcome.sampled) == 6


def test_respects_max_files_cap(tmp_path: Path) -> None:
    files = [_write(tmp_path, f"src/f{i}.py") for i in range(50)]

    outcome = rank_and_sample(
        files=files,
        root=tmp_path,
        import_graph=_empty_graph(files),
        max_files=10,
        test_file_quota_pct=0,
    )

    assert len(outcome.sampled) == 10
    assert outcome.excluded_notable != []


def test_higher_centrality_file_is_preferred_when_capping(tmp_path: Path) -> None:
    hub = _write(tmp_path, "src/hub.py")
    leaves = [_write(tmp_path, f"src/leaf{i}.py") for i in range(10)]
    files = [hub, *leaves]

    graph = ImportGraph(in_degree={hub: 9, **{leaf: 0 for leaf in leaves}}, edges={})

    outcome = rank_and_sample(
        files=files,
        root=tmp_path,
        import_graph=graph,
        max_files=1,
        test_file_quota_pct=0,
    )

    assert [f.path for f in outcome.sampled] == ["src/hub.py"]


def test_test_file_quota_is_guaranteed_even_with_low_centrality(tmp_path: Path) -> None:
    # 8 app files with high centrality, 2 test files with none -- without
    # a guaranteed quota, ranking alone would starve the test files
    # entirely at a tight budget.
    app_files = [_write(tmp_path, f"src/app{i}.py") for i in range(8)]
    test_files = [_write(tmp_path, f"tests/test_app{i}.py") for i in range(2)]
    files = app_files + test_files

    graph = ImportGraph(
        in_degree={**{f: 5 for f in app_files}, **{f: 0 for f in test_files}},
        edges={},
    )

    outcome = rank_and_sample(
        files=files,
        root=tmp_path,
        import_graph=graph,
        max_files=5,
        test_file_quota_pct=20,  # 20% of 5 = 1 slot reserved for tests
    )

    sampled_paths = {f.path for f in outcome.sampled}
    assert any(p.startswith("tests/") for p in sampled_paths)
    assert len(outcome.sampled) == 5


def test_directory_stratified_allocation_spreads_across_modules(tmp_path: Path) -> None:
    # A big `src/` (8 files) and a small `worker/` (2 files). A naive
    # global top-N by count alone (with equal centrality) would be biased
    # by insertion order; stratified allocation should still give
    # `worker/` its proportional share rather than zero.
    src_files = [_write(tmp_path, f"src/f{i}.py") for i in range(8)]
    worker_files = [_write(tmp_path, f"worker/w{i}.py") for i in range(2)]
    files = src_files + worker_files

    outcome = rank_and_sample(
        files=files,
        root=tmp_path,
        import_graph=_empty_graph(files),
        max_files=5,
        test_file_quota_pct=0,
    )

    modules = {f.module for f in outcome.sampled}
    assert "worker" in modules
    assert "src" in modules
    assert len(outcome.sampled) == 5


def test_sampled_file_records_module_and_test_flag(tmp_path: Path) -> None:
    files = [
        _write(tmp_path, "src/app/main.py"),
        _write(tmp_path, "tests/test_main.py"),
    ]

    outcome = rank_and_sample(
        files=files,
        root=tmp_path,
        import_graph=_empty_graph(files),
        max_files=10,
        test_file_quota_pct=10,
    )

    by_path = {f.path: f for f in outcome.sampled}
    assert by_path["src/app/main.py"].module == "src"
    assert by_path["src/app/main.py"].is_test is False
    assert by_path["tests/test_main.py"].module == "tests"
    assert by_path["tests/test_main.py"].is_test is True


def test_excluded_notable_summarizes_by_module(tmp_path: Path) -> None:
    files = [_write(tmp_path, f"src/f{i}.py") for i in range(10)]

    outcome = rank_and_sample(
        files=files,
        root=tmp_path,
        import_graph=_empty_graph(files),
        max_files=3,
        test_file_quota_pct=0,
    )

    assert len(outcome.excluded_notable) == 1
    assert "src" in outcome.excluded_notable[0]
    assert "7" in outcome.excluded_notable[0]


def test_manifest_and_readme_are_always_included_regardless_of_rank(tmp_path: Path) -> None:
    files = [_write(tmp_path, "pyproject.toml"), _write(tmp_path, "README.md")]
    files += [_write(tmp_path, f"src/f{i}.py") for i in range(20)]

    outcome = rank_and_sample(
        files=files,
        root=tmp_path,
        import_graph=_empty_graph(files),
        max_files=1,
        test_file_quota_pct=0,
    )

    always_included_paths = {f.path for f in outcome.sampled if f.always_include}
    assert always_included_paths == {"pyproject.toml", "README.md"}
