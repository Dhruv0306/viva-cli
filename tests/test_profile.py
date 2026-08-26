from __future__ import annotations

from pathlib import Path

from viva.analyzer.models import AnalysisResult, AnalysisStats, ModuleSummary
from viva.ingest.models import ExclusionStats, IngestResult, SampledFile
from viva.profile import ProjectProfile


def test_build_merges_all_fields_from_both_sources():
    ingest_result = IngestResult(
        repo_url="https://github.com/owner/repo",
        repo_slug="owner/repo",
        commit_sha="abc123",
        branch="main",
        local_path=Path("/tmp/clone"),
        files_total=5,
        files_analyzed=5,
        sampled_files=[SampledFile(path="a.py", size_bytes=10, module="")],
        excluded_notable=["1 binary file excluded"],
        sampling_note="analyzed 5/5 files, no sampling needed",
        detected_stack=["python"],
        exclusion_stats=ExclusionStats(excluded_binary=1),
    )
    analysis_result = AnalysisResult(
        architecture_summary="A small python project.",
        modules=[ModuleSummary(module="", summary="root files", file_count=1)],
        entry_points=["a.py"],
        test_coverage_present=False,
        analysis_stats=AnalysisStats(files_analyzed=5, ast_parsed=3, line_window_fallback=2),
    )

    profile = ProjectProfile.build(ingest_result, analysis_result)

    assert profile.repo_slug == "owner/repo"
    assert profile.commit_sha == "abc123"
    assert profile.files_total == 5
    assert profile.sampled_files == ingest_result.sampled_files
    assert profile.excluded_notable == ["1 binary file excluded"]
    assert profile.detected_stack == ["python"]
    assert profile.exclusion_stats.excluded_binary == 1
    assert profile.architecture_summary == "A small python project."
    assert profile.modules[0].module == ""
    assert profile.entry_points == ["a.py"]
    assert profile.test_coverage_present is False
    assert profile.analysis_stats.ast_parsed == 3
