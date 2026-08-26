"""Analyzer component (docs/plan.md Phase 3, docs/design.md §3.1 "Analyzer").

Turns the file list `ingest_repo()` produced into the Project Profile
fields Ingest deliberately left empty (see ingest/models.py's
`IngestResult` docstring): `architecture_summary`, per-module `summary`
text, `entry_points`, and `test_coverage_present`.

Pipeline (FR6-FR8):
  1. `extract` -- per sampled file, tree-sitter AST extraction for the
     language allowlist, or line-window fallback otherwise/on parse
     failure (FR6).
  2. `summarize` -- Map step: one LLM call per file, producing a short
     grounded summary (FR7).
  3. `reduce` -- Reduce step: file summaries -> module summaries ->
     architecture summary, batched and recursed per
     docs/system-design/06-cli-contract-and-profile-scaling.md §6.2 when
     a level's combined text would overflow `MAX_REDUCE_CONTEXT_TOKENS`
     (FR7/FR8).

Public entrypoint: `analyze_repo()`. Per design.md's component rule ("no
service calls another directly"), this is the seam the future
Orchestrator (Phase 6) will call -- everything else in this package is an
internal implementation detail.
"""
from __future__ import annotations

from viva.analyzer.entry_points import detect_entry_points
from viva.analyzer.extract import analyze_file
from viva.analyzer.models import AnalysisResult, AnalysisStats
from viva.analyzer.reduce import build_architecture_summary, reduce_module
from viva.analyzer.summarize import summarize_files
from viva.config import Config
from viva.ingest.models import IngestResult
from viva.llm_client import LLMClient

__all__ = ["analyze_repo"]


def analyze_repo(
    ingest_result: IngestResult,
    config: Config,
    llm_client: LLMClient,
) -> AnalysisResult:
    """Run the full Analysis pipeline over an `IngestResult` (FR6-FR8).

    Reads file contents from `ingest_result.local_path` for every
    `SampledFile`, so this must run after `ingest_repo()` and before the
    raw clone is deleted (NFR7 / design.md §8.2 -- deletion happens after
    `INDEXING`, which is Phase 4, so Analysis is safely before that).
    """
    root = ingest_result.local_path

    file_analyses = []
    stats = AnalysisStats()
    for sampled in ingest_result.sampled_files:
        content = (root / sampled.path).read_text(encoding="utf-8", errors="replace")
        analysis = analyze_file(
            path=sampled.path,
            content=content,
            module=sampled.module,
            line_window_size=config.line_window_size,
            line_window_overlap=config.line_window_overlap,
        )
        file_analyses.append(analysis)
        stats.record(analysis)

    file_summaries = summarize_files(file_analyses, llm_client)

    modules_by_name: dict[str, list] = {}
    for fs in file_summaries:
        modules_by_name.setdefault(fs.module, []).append(fs)

    module_summaries = [
        reduce_module(module, summaries, llm_client, config)
        for module, summaries in sorted(modules_by_name.items())
    ]

    architecture_summary = build_architecture_summary(module_summaries, llm_client, config)

    entry_points = detect_entry_points(ingest_result.sampled_files, ingest_result.detected_stack)
    test_coverage_present = any(f.is_test for f in ingest_result.sampled_files)

    return AnalysisResult(
        architecture_summary=architecture_summary,
        modules=module_summaries,
        entry_points=entry_points,
        test_coverage_present=test_coverage_present,
        analysis_stats=stats,
    )
