"""Pass B: priority ranking + capping (docs/design.md §3,
01-resolved-decisions.md §1.1).

Three tiers, in order:

1. **Always-include** -- README, entry points, manifests. Outside the
   MAX_FILES budget entirely; these are structural context the Analyzer
   needs regardless of repo size (design.md §3).
2. **Guaranteed test-file quota** -- TEST_FILE_QUOTA_PCT of the remaining
   budget is reserved for test files specifically, so that category
   doesn't silently disappear behind higher-centrality application code
   in a large repo.
3. **Directory-stratified allocation** -- the rest of the budget is spread
   proportionally across top-level modules (rather than globally
   top-N-by-score), so a large `src/` doesn't crowd out a smaller but
   still-relevant `worker/` or `cli/` directory. Within each module's
   slice, files are chosen by import centrality and path heuristics.

Only engages if the filtered file count exceeds MAX_FILES; otherwise
everything is analyzed and this is a no-op pass-through.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from viva.ingest.models import ImportGraph, SampledFile

_MANIFEST_NAMES = frozenset(
    {
        "package.json",
        "requirements.txt",
        "pyproject.toml",
        "setup.py",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "go.mod",
        "cargo.toml",
        "gemfile",
        "composer.json",
    }
)
_ALWAYS_INCLUDE_STEMS = frozenset({"readme", "manage", "app", "main", "index"})

_TEST_DIR_NAMES = frozenset({"test", "tests", "__tests__", "spec", "specs"})
_TEST_NAME_PATTERN = re.compile(r"(^test_|_test\.|\.test\.|\.spec\.|^spec_|_spec\.)")

_HIGH_PRIORITY_DIR_NAMES = frozenset({"src", "core", "lib", "app"})
_LOW_PRIORITY_DIR_NAMES = frozenset({"scripts", "examples", "docs", "sample", "samples"})


def _is_always_include(f: Path) -> bool:
    name_lower = f.name.lower()
    if name_lower in _MANIFEST_NAMES or name_lower.endswith(".csproj"):
        return True
    return f.stem.lower() in _ALWAYS_INCLUDE_STEMS


def _is_test_file(f: Path, root: Path) -> bool:
    parts = f.relative_to(root).parts[:-1]
    if any(p.lower() in _TEST_DIR_NAMES for p in parts):
        return True
    return bool(_TEST_NAME_PATTERN.search(f.name.lower()))


def _top_level_module(f: Path, root: Path) -> str:
    parts = f.relative_to(root).parts
    return parts[0] if len(parts) > 1 else ""


def _path_heuristic_score(rel_dir_parts: tuple[str, ...]) -> int:
    top = rel_dir_parts[0] if rel_dir_parts else ""
    if top in _HIGH_PRIORITY_DIR_NAMES:
        return 2
    if top in _LOW_PRIORITY_DIR_NAMES:
        return -1
    return 0


def _rank(files: list[Path], root: Path, graph: ImportGraph) -> list[Path]:
    def score(f: Path) -> int:
        rel_dir_parts = f.relative_to(root).parts[:-1]
        return graph.centrality(f) * 2 + _path_heuristic_score(rel_dir_parts)

    # Stable sort: ties keep the original (os.walk) order as a
    # deterministic tiebreaker rather than depending on dict/set ordering.
    return sorted(files, key=score, reverse=True)


def _directory_stratified_allocate(ranked: list[Path], root: Path, budget: int) -> list[Path]:
    """Allocate `budget` slots proportionally across top-level modules,
    using each module's already-ranked file order so the highest-scoring
    files within a module are the ones kept when its slice is smaller
    than its total file count.
    """
    if budget <= 0 or not ranked:
        return []

    groups: dict[str, list[Path]] = defaultdict(list)
    for f in ranked:
        groups[_top_level_module(f, root)].append(f)

    total = len(ranked)
    take_count: dict[str, int] = {}
    fractional: list[tuple[float, str]] = []

    for module, group_files in groups.items():
        exact_share = budget * (len(group_files) / total)
        base = min(int(exact_share), len(group_files))
        take_count[module] = base
        fractional.append((exact_share - base, module))

    leftover = budget - sum(take_count.values())

    # Largest-remainder apportionment: give leftover slots to the modules
    # with the biggest rounding-down loss first, so the total lands
    # exactly on `budget` instead of under-allocating from truncation.
    fractional.sort(key=lambda r: r[0], reverse=True)
    for _, module in fractional:
        if leftover <= 0:
            break
        if take_count[module] < len(groups[module]):
            take_count[module] += 1
            leftover -= 1

    selected: list[Path] = []
    for module, group_files in groups.items():
        selected.extend(group_files[: take_count[module]])
    return selected


def _to_sampled_file(f: Path, root: Path, *, always_include: bool) -> SampledFile:
    return SampledFile(
        path=f.relative_to(root).as_posix(),
        size_bytes=f.stat().st_size,
        module=_top_level_module(f, root),
        always_include=always_include,
        is_test=_is_test_file(f, root),
    )


def _summarize_excluded(excluded: list[Path], root: Path) -> list[str]:
    if not excluded:
        return []
    by_module: dict[str, int] = defaultdict(int)
    for f in excluded:
        by_module[_top_level_module(f, root) or "(root)"] += 1
    return [f"{count} file(s) sampled out of {module}" for module, count in sorted(by_module.items())]


@dataclass
class SamplingOutcome:
    sampled: list[SampledFile]
    excluded_notable: list[str]
    sampling_note: str


def rank_and_sample(
    files: list[Path],
    root: Path,
    import_graph: ImportGraph,
    max_files: int,
    test_file_quota_pct: int,
) -> SamplingOutcome:
    """Apply Pass B to a hard-exclusion-filtered file set."""
    always_include: list[Path] = []
    candidates: list[Path] = []
    for f in files:
        (always_include if _is_always_include(f) else candidates).append(f)

    files_total = len(files)

    if len(candidates) + len(always_include) <= max_files:
        sampled = [_to_sampled_file(f, root, always_include=True) for f in always_include]
        sampled += [_to_sampled_file(f, root, always_include=False) for f in candidates]
        return SamplingOutcome(
            sampled=sampled,
            excluded_notable=[],
            sampling_note=f"analyzed {files_total}/{files_total} files, no sampling needed",
        )

    # Always-include files are outside the budget entirely (see module
    # docstring) -- the full max_files budget is available to candidates.
    budget = max_files

    test_files = [f for f in candidates if _is_test_file(f, root)]
    test_set = set(test_files)
    non_test_files = [f for f in candidates if f not in test_set]

    test_quota = min(len(test_files), round(budget * test_file_quota_pct / 100))
    remaining_budget = budget - test_quota

    selected_tests = _rank(test_files, root, import_graph)[:test_quota]
    ranked_non_test = _rank(non_test_files, root, import_graph)
    selected_non_test = _directory_stratified_allocate(ranked_non_test, root, remaining_budget)

    selected = set(selected_tests) | set(selected_non_test)

    sampled = [_to_sampled_file(f, root, always_include=True) for f in always_include]
    sampled += [_to_sampled_file(f, root, always_include=False) for f in candidates if f in selected]

    excluded = [f for f in candidates if f not in selected]
    excluded_notable = _summarize_excluded(excluded, root)

    sampling_note = (
        f"analyzed {len(sampled)}/{files_total} files, prioritized by import "
        f"centrality, directory-stratified allocation, and a "
        f"{test_file_quota_pct}% guaranteed test-file quota"
    )

    return SamplingOutcome(sampled=sampled, excluded_notable=excluded_notable, sampling_note=sampling_note)
