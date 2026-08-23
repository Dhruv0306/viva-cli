"""Data contracts for the Ingest component (docs/plan.md Phase 2).

These are intentionally a *subset* of the full Project Profile
(docs/design.md §6): `IngestResult` covers only what Ingest itself
produces (repo identity, the sampled file set, exclusion transparency,
detected stack). `architecture_summary`, per-module `summary` text, and
`test_coverage_present` are populated by the Analyzer (Phase 3) from the
files sampled here -- Ingest's job ends at "here is the correct,
transparently-sampled file list," not project understanding.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SampledFile:
    """One file that survived both filtering passes (docs/design.md §3).

    `module` is the top-level directory group used for directory-stratified
    allocation (01-resolved-decisions.md §1.1) -- kept on the record itself
    so downstream consumers (e.g. the Analyzer's module grouping in Phase 3)
    don't have to re-derive it from `path`.
    """

    path: str  # relative to repo root, POSIX-style separators
    size_bytes: int
    module: str  # top-level directory name, or "" for repo-root files
    always_include: bool = False  # README/entry point/manifest -- outside the MAX_FILES budget
    is_test: bool = False  # counted against TEST_FILE_QUOTA_PCT rather than the general budget


@dataclass
class ExclusionStats:
    """Pass A (hard exclusion) bookkeeping.

    Mutable, unlike the rest of this module -- it's accumulated
    incrementally during the directory walk in `filters.py` rather than
    constructed once with final values.
    """

    excluded_dirs: int = 0
    excluded_binary: int = 0
    excluded_lockfile: int = 0
    excluded_oversized: int = 0


@dataclass(frozen=True)
class ImportGraph:
    """Cheap, regex-derived import graph used only to rank files for
    sampling (see `import_graph.py`). Not a real dependency graph --
    resolution is best-effort suffix matching, not a language-aware
    linker.
    """

    in_degree: dict[Path, int]
    edges: dict[Path, set[Path]]

    def centrality(self, path: Path) -> int:
        return self.in_degree.get(path, 0)


@dataclass(frozen=True)
class IngestResult:
    """Ingest component output (docs/plan.md Phase 2).

    Feeds directly into the still-empty fields of the Project Profile
    (docs/design.md §6) that Phase 3's Analyzer will complete:
    `files_analyzed`, `files_total`, `sampling_note`, `detected_stack`,
    `excluded_notable`. `commit_sha` is pinned here and never re-resolved
    later (05-repo-lifecycle-and-language-coverage.md §5.2-5.3) -- it's
    what will key the eventual Chroma collection.
    """

    repo_url: str
    repo_slug: str
    commit_sha: str
    branch: str
    local_path: Path
    files_total: int
    files_analyzed: int
    sampled_files: list[SampledFile]
    excluded_notable: list[str]
    sampling_note: str
    detected_stack: list[str]
    exclusion_stats: ExclusionStats = field(default_factory=ExclusionStats)
