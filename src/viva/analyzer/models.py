"""Data contracts for the Analyzer component (docs/plan.md Phase 3).

Mirrors the split already established by `ingest/models.py`: small,
frozen dataclasses for the pipeline's intermediate stages, plus one
mutable stats accumulator (`AnalysisStats`, mirroring Phase 2's
`ExclusionStats`) collected incrementally as files are processed.

`AnalysisResult` is the Analyzer's half of the Project Profile
(docs/design.md §6) -- `ProjectProfile.build()` in `viva/profile.py`
merges this with Ingest's `IngestResult` half into the single object the
rest of the pipeline reads.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ParseMethod = Literal["ast", "line_window"]


@dataclass(frozen=True)
class CodeUnit:
    """One function/method/class extracted via tree-sitter AST parsing
    (FR6). Absent entirely for `line_window`-fallback files -- there's no
    AST to pull units from in that case, see `FileAnalysis.raw_windows`.
    """

    kind: Literal["function", "class"]
    name: str
    signature: str  # first line of the node's text, e.g. "def foo(a, b):"
    docstring: str | None
    start_line: int
    end_line: int
    # First few lines of the body, truncated -- only meaningfully used by
    # the Map step (summarize.py) when `docstring` is None, since a bare
    # signature with no docstring and no body excerpt gives the
    # summarizing LLM call nothing to actually describe.
    body_excerpt: str = ""


@dataclass(frozen=True)
class FileAnalysis:
    """Per-file extraction result -- either AST code units or line-window
    text chunks, tagged by `parse_method` (05-repo-lifecycle-and-language-coverage.md
    §5.1). Exactly one of `units` / `raw_windows` is non-empty.
    """

    path: str
    module: str
    language: str | None  # None when the extension isn't in the allowlist at all
    parse_method: ParseMethod
    units: list[CodeUnit] = field(default_factory=list)
    raw_windows: list[str] = field(default_factory=list)
    # Set only when `parse_method == "line_window"` *because* an
    # in-allowlist language raised during AST extraction (as opposed to
    # parsing fine but matching no units, or the extension simply being
    # outside the allowlist) -- lets AnalysisStats distinguish a real
    # parse failure from an expected fallback, and lets callers log the
    # actual cause instead of a silent fallback (see extract.py).
    parse_error: str | None = None


@dataclass(frozen=True)
class FileSummary:
    """Map-step output: one LLM-produced summary per sampled file (FR7)."""

    path: str
    module: str
    parse_method: ParseMethod
    summary: str


@dataclass(frozen=True)
class ModuleSummary:
    """Reduce-step output for one module (top-level directory group,
    matching `SampledFile.module` from Phase 2's sampling)."""

    module: str
    summary: str
    file_count: int


@dataclass
class AnalysisStats:
    """Analysis-pipeline bookkeeping, mirroring Phase 2's `ExclusionStats`
    -- accumulated incrementally via `record()` as each file is analyzed,
    feeding the same kind of transparency note design.md §8.1 gives as an
    example: "12 files fell back to line-window chunking."
    """

    files_analyzed: int = 0
    ast_parsed: int = 0
    line_window_fallback: int = 0
    parse_failures_by_language: dict[str, int] = field(default_factory=dict)

    def record(self, analysis: FileAnalysis) -> None:
        self.files_analyzed += 1
        if analysis.parse_method == "ast":
            self.ast_parsed += 1
        else:
            self.line_window_fallback += 1
        if analysis.parse_error and analysis.language:
            self.parse_failures_by_language[analysis.language] = (
                self.parse_failures_by_language.get(analysis.language, 0) + 1
            )

    def summary_note(self) -> str | None:
        if self.line_window_fallback == 0:
            return None
        return f"{self.line_window_fallback} file(s) fell back to line-window chunking"


@dataclass(frozen=True)
class AnalysisResult:
    """Analyzer component output (docs/plan.md Phase 3) -- the fields of
    the Project Profile (docs/design.md §6) that Ingest left for this
    component to fill in.
    """

    architecture_summary: str
    modules: list[ModuleSummary]
    entry_points: list[str]
    test_coverage_present: bool
    analysis_stats: AnalysisStats
