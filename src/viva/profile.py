"""The Project Profile (docs/design.md §6, FR8): the single object that
merges Ingest's `IngestResult` with the Analyzer's `AnalysisResult`.

Lives at the top level, sibling to `config.py`/`schemas.py`, deliberately
not owned by either `ingest/` or `analyzer/` -- FR8 requires it be
"stored separately from the retrieval index and injectable into any LLM
call," which makes it a first-class pipeline artifact in its own right,
not a private detail of either component that produces half of it.
Every later phase (RAG indexing, question generation, evaluation,
reporting) reads this object, not `IngestResult`/`AnalysisResult`
directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from viva.analyzer.models import AnalysisResult, AnalysisStats, ModuleSummary
from viva.ingest.models import ExclusionStats, IngestResult, SampledFile


@dataclass(frozen=True)
class ProjectProfile:
    """The full Project Profile (docs/design.md §6)."""

    # --- identity (from IngestResult) ---
    repo_url: str
    repo_slug: str
    commit_sha: str
    branch: str
    local_path: Path

    # --- sampling transparency (from IngestResult) ---
    files_total: int
    files_analyzed: int
    sampled_files: list[SampledFile]
    excluded_notable: list[str]
    sampling_note: str
    detected_stack: list[str]
    exclusion_stats: ExclusionStats

    # --- analysis (from AnalysisResult) ---
    architecture_summary: str
    modules: list[ModuleSummary]
    entry_points: list[str]
    test_coverage_present: bool
    analysis_stats: AnalysisStats

    @classmethod
    def build(cls, ingest_result: IngestResult, analysis_result: AnalysisResult) -> "ProjectProfile":
        return cls(
            repo_url=ingest_result.repo_url,
            repo_slug=ingest_result.repo_slug,
            commit_sha=ingest_result.commit_sha,
            branch=ingest_result.branch,
            local_path=ingest_result.local_path,
            files_total=ingest_result.files_total,
            files_analyzed=ingest_result.files_analyzed,
            sampled_files=ingest_result.sampled_files,
            excluded_notable=ingest_result.excluded_notable,
            sampling_note=ingest_result.sampling_note,
            detected_stack=ingest_result.detected_stack,
            exclusion_stats=ingest_result.exclusion_stats,
            architecture_summary=analysis_result.architecture_summary,
            modules=analysis_result.modules,
            entry_points=analysis_result.entry_points,
            test_coverage_present=analysis_result.test_coverage_present,
            analysis_stats=analysis_result.analysis_stats,
        )
