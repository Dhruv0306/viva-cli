"""Ingest component (docs/plan.md Phase 2, docs/design.md §1 "Ingest").

Clones a repo, applies hard exclusion + priority sampling, and detects the
primary tech stack, producing an `IngestResult` that feeds the Project
Profile fields the Analyzer (Phase 3) doesn't own (`files_analyzed`,
`files_total`, `sampling_note`, `detected_stack`, `excluded_notable`).

Public entrypoint: `ingest_repo()`. Per design.md's component rule ("no
service calls another directly"), this is the seam the future
Orchestrator (Phase 6) will call -- everything else in this package
(`clone`, `filters`, `import_graph`, `sampling`, `stack`) is an internal
implementation detail and shouldn't be imported directly from outside it.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from viva.config import Config
from viva.ingest.clone import clone_repo
from viva.ingest.filters import walk_and_hard_exclude
from viva.ingest.import_graph import build_import_graph
from viva.ingest.models import IngestResult
from viva.ingest.sampling import rank_and_sample
from viva.ingest.stack import detect_stack

__all__ = ["ingest_repo"]


def ingest_repo(
    repo_url: str,
    config: Config,
    branch: str | None = None,
    work_dir: Path | None = None,
) -> IngestResult:
    """Clone, filter, sample, and stack-detect a repo end-to-end (FR1-FR5).

    `work_dir` defaults to a fresh temp directory. It is deliberately
    *not* deleted here -- per NFR7 / design.md §8.2, the raw clone is
    deleted once the `INDEXING` state completes, which is Phase 4's
    responsibility, not Ingest's. Ingest's contract ends at producing a
    correct, transparent file list for the Analyzer to read from
    `local_path`.
    """
    dest = work_dir or Path(tempfile.mkdtemp(prefix="viva-ingest-"))

    cloned = clone_repo(
        repo_url=repo_url,
        dest_dir=dest,
        branch=branch,
        github_token=config.github_token,
    )

    filter_outcome = walk_and_hard_exclude(cloned.local_path)
    import_graph = build_import_graph(filter_outcome.kept, cloned.local_path)
    sampling_outcome = rank_and_sample(
        files=filter_outcome.kept,
        root=cloned.local_path,
        import_graph=import_graph,
        max_files=config.max_files,
        test_file_quota_pct=config.test_file_quota_pct,
    )
    detected_stack = detect_stack(filter_outcome.kept, cloned.local_path)

    excluded_notable = list(sampling_outcome.excluded_notable)
    stats = filter_outcome.stats
    if stats.excluded_dirs:
        excluded_notable.insert(0, f"{stats.excluded_dirs} excluded director(y/ies) (VCS/deps/build)")
    if stats.excluded_binary:
        excluded_notable.append(f"{stats.excluded_binary} binary/unreadable file(s) excluded")
    if stats.excluded_lockfile:
        excluded_notable.append(f"{stats.excluded_lockfile} lockfile(s) excluded")
    if stats.excluded_oversized:
        excluded_notable.append(f"{stats.excluded_oversized} oversized file(s) excluded")

    return IngestResult(
        repo_url=repo_url,
        repo_slug=cloned.repo_slug,
        commit_sha=cloned.commit_sha,
        branch=cloned.branch,
        local_path=cloned.local_path,
        files_total=len(filter_outcome.kept),
        files_analyzed=len(sampling_outcome.sampled),
        sampled_files=sampling_outcome.sampled,
        excluded_notable=excluded_notable,
        sampling_note=sampling_outcome.sampling_note,
        detected_stack=detected_stack,
        exclusion_stats=stats,
    )
