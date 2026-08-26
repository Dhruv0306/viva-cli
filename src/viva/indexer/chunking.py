"""FR9: function/class-granularity chunking with metadata.

Re-runs `analyzer.extract.analyze_file()` (Phase 3's tree-sitter
extraction) against the still-on-disk repo rather than reusing Phase 3's
`CodeUnit.body_excerpt` -- see
docs/system-design/09-phase-4-indexing-design.md §9.1 for the full
rationale: the excerpt is conditionally truncated (built for the Map
step's summarization prompt, not for embedding), so full-fidelity chunk
text is re-sliced from the source file directly using each unit's
`start_line`/`end_line`. This must run before the raw clone is deleted
(NFR7 / design.md §8.2 -- deletion happens after `INDEXING` completes),
same ordering constraint `analyzer.analyze_repo()` already documents for
`ANALYZING`.

Only ever called with `ProjectProfile.sampled_files` -- excluded/
sampled-out files are never indexed (design.md §3: "the Question
Generator is not permitted to target excluded files").
"""
from __future__ import annotations

from pathlib import Path

from viva.analyzer.extract import analyze_file
from viva.analyzer.models import CodeUnit, FileAnalysis
from viva.config import Config
from viva.indexer.models import Chunk
from viva.ingest.models import SampledFile


def build_chunks(
    sampled_files: list[SampledFile],
    root: Path,
    repo_slug: str,
    commit_sha: str,
    config: Config,
) -> list[Chunk]:
    """Chunk every sampled file, in `sampled_files` order.

    Chunks from the same file are always contiguous in the returned
    list (one file fully processed before the next starts) -- callers
    that want to batch downstream work (e.g. embedding) per file, per
    docs/system-design/09-phase-4-indexing-design.md §9.6, can rely on
    this without re-sorting.
    """
    chunks: list[Chunk] = []
    for sampled in sampled_files:
        content = (root / sampled.path).read_text(encoding="utf-8", errors="replace")
        analysis = analyze_file(
            path=sampled.path,
            content=content,
            module=sampled.module,
            line_window_size=config.line_window_size,
            line_window_overlap=config.line_window_overlap,
        )
        chunks.extend(_chunks_for_file(analysis, content, repo_slug, commit_sha, config))
    return chunks


def _chunks_for_file(
    analysis: FileAnalysis, content: str, repo_slug: str, commit_sha: str, config: Config
) -> list[Chunk]:
    if analysis.parse_method == "ast":
        return [_chunk_for_unit(analysis, unit, content, repo_slug, commit_sha) for unit in analysis.units]

    return [
        _chunk_for_window(analysis, idx, window, repo_slug, commit_sha, config)
        for idx, window in enumerate(analysis.raw_windows)
    ]


def _chunk_for_unit(
    analysis: FileAnalysis, unit: CodeUnit, content: str, repo_slug: str, commit_sha: str
) -> Chunk:
    # start_line/end_line are 1-indexed, inclusive (analyzer/extract.py).
    lines = content.splitlines()
    text = "\n".join(lines[unit.start_line - 1 : unit.end_line])
    return Chunk(
        id=f"{repo_slug}-{commit_sha}-{analysis.path}-{unit.start_line}",
        text=text,
        filepath=analysis.path,
        module=analysis.module,
        symbol_name=unit.name,
        kind=unit.kind,
        parse_method="ast",
        language=analysis.language,
        start_line=unit.start_line,
        end_line=unit.end_line,
    )


def _chunk_for_window(
    analysis: FileAnalysis, idx: int, window_text: str, repo_slug: str, commit_sha: str, config: Config
) -> Chunk:
    # raw_windows (extract.py's _line_window_fallback) don't carry their
    # own line range -- recompute the same window's start line from its
    # index and the step size that generated it (window_size - overlap),
    # so line_window chunks get a real citable line range too, not just
    # AST ones. This only stays correct as long as the same
    # line_window_size/overlap config used to produce `analysis` is
    # passed in here, which build_chunks() guarantees.
    step = max(config.line_window_size - config.line_window_overlap, 1)
    start_line = idx * step + 1
    end_line = start_line + len(window_text.splitlines()) - 1
    return Chunk(
        id=f"{repo_slug}-{commit_sha}-{analysis.path}-{start_line}",
        text=window_text,
        filepath=analysis.path,
        module=analysis.module,
        symbol_name=None,
        kind="line_window",
        parse_method="line_window",
        language=analysis.language,
        start_line=start_line,
        end_line=end_line,
    )
