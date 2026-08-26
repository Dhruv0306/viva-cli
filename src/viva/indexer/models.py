"""Data contracts for the Indexer/RAG component (docs/plan.md Phase 4).

`Chunk` is FR9's function/class-granularity code unit plus the metadata
FR9/FR11 need (filepath, symbol name, module role via `module`). One
`Chunk` is produced per `analyzer.models.CodeUnit` for AST-parsed files,
or per `raw_windows` entry for `line_window`-fallback files -- see
`indexer/chunking.py`.

`start_line`/`end_line` are carried on every chunk (not just AST ones)
so they can double as the citation anchor FR22/NFR4 need downstream
(`cited_file`, e.g. `src/payments/handler.py:42`) -- reusing the same
line-range shape `analyzer.models.CodeUnit` already established rather
than inventing a second one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from viva.analyzer.models import ParseMethod

ChunkKind = Literal["function", "class", "line_window"]


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str
    filepath: str
    module: str
    symbol_name: str | None  # None for line_window chunks -- no real symbol boundary to name
    kind: ChunkKind
    parse_method: ParseMethod
    language: str | None
    start_line: int
    end_line: int


@dataclass
class IndexStats:
    """Bookkeeping mirroring Phase 2/3's `ExclusionStats`/`AnalysisStats`
    pattern, feeding the same kind of NFR8 transparency."""

    files_processed: int = 0
    chunks_built: int = 0
    reused_existing_collection: bool = False


@dataclass(frozen=True)
class IndexResult:
    """Indexer component output (docs/plan.md Phase 4)."""

    collection_name: str
    stats: IndexStats
