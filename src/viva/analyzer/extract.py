"""FR6: per-file structured extraction.

AST extraction via the query-per-language allowlist in `languages.py`
when the file's extension is covered and parsing succeeds; line-window
chunking (05-repo-lifecycle-and-language-coverage.md §5.1's fallback,
default 60 lines / 15-line overlap, now `Config.line_window_size` /
`Config.line_window_overlap` per the FR28 tunable-everything convention)
for anything else -- an unrecognized extension, or an in-allowlist file
that fails to parse (syntax errors, encoding issues, grammar edge cases).

Every result is tagged with `parse_method` so the rest of the pipeline
(and the eventual Project Profile's `analysis_stats`) can be honest about
which files got real structural understanding versus a text-window
approximation (NFR8 transparency).
"""
from __future__ import annotations

import logging

from tree_sitter import Node, Query, QueryCursor
from tree_sitter_language_pack import get_language, get_parser

from viva.analyzer.languages import load_query_source, resolve_language
from viva.analyzer.models import CodeUnit, FileAnalysis

logger = logging.getLogger(__name__)

_BODY_EXCERPT_MAX_CHARS = 800

# Query files use two capture families: definition.<kind> on the whole
# node, name.<kind> on just the identifier -- see queries/*.scm.
_KIND_CAPTURES = {"definition.function": "function", "definition.class": "class"}


def analyze_file(
    path: str,
    content: str,
    module: str,
    line_window_size: int,
    line_window_overlap: int,
) -> FileAnalysis:
    resolved = resolve_language(path)
    if resolved is None:
        return _line_window_fallback(path, module, content, None, line_window_size, line_window_overlap)

    language_key, query_stem = resolved
    try:
        units = _extract_ast_units(content, language_key, query_stem)
    except Exception as exc:
        # Any parse/query failure (malformed source, grammar edge case,
        # encoding surprise, a missing platform binary for this
        # language) falls back rather than aborting the whole analysis
        # run over one file -- design.md §4/§9's "never a hard failure"
        # principle applies here too, not just to LLM calls. Logged at
        # WARNING with the full traceback rather than swallowed silently
        # -- a systemic cause (e.g. every language failing the same way)
        # is invisible in `analysis_stats` alone and needs to actually
        # surface somewhere.
        logger.warning(
            "AST extraction failed for %s (language=%s), falling back to line-window: %s",
            path, language_key, exc, exc_info=True,
        )
        return _line_window_fallback(
            path, module, content, language_key, line_window_size, line_window_overlap, parse_error=str(exc)
        )

    if not units:
        # Parsed fine, but nothing matched the query (e.g. a file that's
        # all top-level statements, no functions/classes) -- line-window
        # still gives the Map step something to summarize. Not a parse
        # error, so `parse_error` stays unset.
        return _line_window_fallback(
            path, module, content, language_key, line_window_size, line_window_overlap
        )

    return FileAnalysis(
        path=path,
        module=module,
        language=language_key,
        parse_method="ast",
        units=units,
    )


def _extract_ast_units(content: str, language_key: str, query_stem: str) -> list[CodeUnit]:
    parser = get_parser(language_key)
    language = get_language(language_key)
    source = content.encode("utf-8", errors="replace")
    tree = parser.parse(source)

    query = Query(language, load_query_source(query_stem))
    cursor = QueryCursor(query)
    matches = cursor.matches(tree.root_node)

    units: list[CodeUnit] = []
    for _pattern_idx, captures in matches:
        for capture_name, kind in _KIND_CAPTURES.items():
            def_nodes = captures.get(capture_name)
            if not def_nodes:
                continue
            def_node = def_nodes[0]
            name_nodes = captures.get(f"name.{kind}", [])
            name = name_nodes[0].text.decode("utf-8", errors="replace") if name_nodes else "<anonymous>"
            units.append(_build_code_unit(def_node, kind, name, language_key))
    return units


def _build_code_unit(def_node: Node, kind: str, name: str, language_key: str) -> CodeUnit:
    node_text = def_node.text.decode("utf-8", errors="replace")
    signature = node_text.splitlines()[0].strip() if node_text else ""
    docstring = _extract_docstring(def_node, language_key)
    body_excerpt = "" if docstring else node_text[:_BODY_EXCERPT_MAX_CHARS]

    return CodeUnit(
        kind=kind,
        name=name,
        signature=signature,
        docstring=docstring,
        start_line=def_node.start_point[0] + 1,
        end_line=def_node.end_point[0] + 1,
        body_excerpt=body_excerpt,
    )


def _extract_docstring(def_node: Node, language_key: str) -> str | None:
    if language_key == "python":
        for child in def_node.children:
            if child.type == "block":
                for stmt in child.children:
                    if stmt.type == "string":
                        return _clean_python_docstring(stmt.text.decode("utf-8", errors="replace"))
                    break  # only the block's first statement counts as a docstring
        return None

    # Every other grammar in this allowlist uses "comment" as the node
    # type for both line and block comments -- treat an immediately
    # preceding comment sibling as a doc-comment (JSDoc/Javadoc/rustdoc
    # convention), same idea as Python's docstring, different syntax.
    prev = def_node.prev_sibling
    if prev is not None and prev.type == "comment":
        return _clean_comment(prev.text.decode("utf-8", errors="replace"))
    return None


def _clean_python_docstring(raw: str) -> str:
    text = raw.strip()
    for quote in ('"""', "'''", '"', "'"):
        if text.startswith(quote) and text.endswith(quote) and len(text) >= 2 * len(quote):
            return text[len(quote) : -len(quote)].strip()
    return text


def _clean_comment(raw: str) -> str:
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        for prefix in ("///", "//!", "//", "/**", "/*", "*/", "*", "#"):
            if line.startswith(prefix):
                line = line[len(prefix) :].strip()
                break
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def _line_window_fallback(
    path: str,
    module: str,
    content: str,
    language_key: str | None,
    window_size: int,
    overlap: int,
    parse_error: str | None = None,
) -> FileAnalysis:
    lines = content.splitlines()
    if not lines:
        return FileAnalysis(
            path=path, module=module, language=language_key, parse_method="line_window",
            raw_windows=[], parse_error=parse_error,
        )

    step = max(window_size - overlap, 1)
    windows = []
    start = 0
    while start < len(lines):
        end = min(start + window_size, len(lines))
        windows.append("\n".join(lines[start:end]))
        if end == len(lines):
            break
        start += step

    return FileAnalysis(
        path=path,
        module=module,
        language=language_key,
        parse_method="line_window",
        raw_windows=windows,
        parse_error=parse_error,
    )
