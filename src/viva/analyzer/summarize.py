"""FR7 Map step: one LLM call per analyzed file, producing a short
grounded summary. Sequential, not concurrent, for v1 -- Ollama serves one
local model at a time on typical hardware, so concurrency here wouldn't
reduce wall-clock time and adds complexity docs/plan.md's Phase 3 scope
doesn't call for.
"""
from __future__ import annotations

from viva.analyzer.models import CodeUnit, FileAnalysis, FileSummary
from viva.llm_client import LLMClient

_TARGET_SUMMARY_TOKENS = 150
_LINE_WINDOW_EXCERPT_MAX_WINDOWS = 2
_BODY_EXCERPT_CHARS_IN_PROMPT = 300


def summarize_files(file_analyses: list[FileAnalysis], llm_client: LLMClient) -> list[FileSummary]:
    return [_summarize_one(fa, llm_client) for fa in file_analyses]


def _summarize_one(fa: FileAnalysis, llm_client: LLMClient) -> FileSummary:
    excerpt = _build_excerpt(fa)
    summary = llm_client.summarize_file(
        path=fa.path,
        language=fa.language,
        content_excerpt=excerpt,
        target_tokens=_TARGET_SUMMARY_TOKENS,
    )
    return FileSummary(path=fa.path, module=fa.module, parse_method=fa.parse_method, summary=summary)


def _build_excerpt(fa: FileAnalysis) -> str:
    if fa.parse_method == "ast":
        if not fa.units:
            return "(no functions or classes found)"
        return "\n\n".join(_render_unit(u) for u in fa.units)

    # line_window fallback: the first couple of windows already cover the
    # top of the file, which is usually enough for a Map-step summary --
    # feeding every window for a large fallback file would blow the
    # per-call prompt budget for no real summary-quality benefit.
    windows = fa.raw_windows[:_LINE_WINDOW_EXCERPT_MAX_WINDOWS]
    return "\n...\n".join(windows) if windows else "(empty file)"


def _render_unit(unit: CodeUnit) -> str:
    lines = [f"{unit.kind} {unit.name}: {unit.signature}"]
    if unit.docstring:
        lines.append(f'    """{unit.docstring}"""')
    elif unit.body_excerpt:
        lines.append(f"    {unit.body_excerpt[:_BODY_EXCERPT_CHARS_IN_PROMPT]}")
    return "\n".join(lines)
