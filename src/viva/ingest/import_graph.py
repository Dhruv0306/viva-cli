"""Cheap, LLM-free import-statement scan used to build file centrality
scores for sampling (docs/design.md §3, 01-resolved-decisions.md §1.1).

Deliberately regex-based, not AST-based -- real AST parsing (tree-sitter)
is Phase 3 scope (FR6). This only needs to be accurate enough to rank
files by "how many other files reference this one," not to extract
structured code units.

Must run over the *full* hard-exclusion-filtered set, before any capping:
centrality can't be computed correctly from an already-capped file set,
since the files that would explain a hub file's centrality might be
exactly the ones a naive cap would have dropped first.

Resolution is intentionally generic rather than one bespoke resolver per
language: a raw import reference (`com.acme.util`, `../models/user`,
`crate::foo::bar`) is normalized into path-like segments and matched
against the *suffix* of real repo file paths, trying the most specific
(longest) suffix first and falling back to shorter ones. A match is only
accepted when it's unambiguous (exactly one candidate) -- an ambiguous
suffix is simply left unresolved rather than guessed at, since a wrong
edge would corrupt the ranking signal more than a missing one.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from viva.ingest.models import ImportGraph

_JS_IMPORT = re.compile(r"""(?:import\s+.*?from\s+|require\()\s*['"](.+?)['"]""", re.MULTILINE)
_C_INCLUDE = re.compile(r'^\s*#include\s*["<](.+?)[">]', re.MULTILINE)

# One pattern per language family, keyed by file extension. Each pattern
# has exactly one non-None capture group per match -- the raw
# module/path reference as written in the source.
_IMPORT_PATTERNS: dict[str, re.Pattern[str]] = {
    ".py": re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.MULTILINE),
    ".js": _JS_IMPORT,
    ".jsx": _JS_IMPORT,
    ".ts": _JS_IMPORT,
    ".tsx": _JS_IMPORT,
    ".java": re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+);", re.MULTILINE),
    ".rs": re.compile(r"^\s*use\s+([\w:]+)", re.MULTILINE),
    ".rb": re.compile(r"""require(?:_relative)?\s*['"](.+?)['"]""", re.MULTILINE),
    ".cs": re.compile(r"^\s*using\s+([\w.]+);", re.MULTILINE),
    ".c": _C_INCLUDE,
    ".h": _C_INCLUDE,
    ".cpp": _C_INCLUDE,
    ".hpp": _C_INCLUDE,
    ".cc": _C_INCLUDE,
}

# Go imports are handled separately (not via _IMPORT_PATTERNS) because a
# single quoted-string regex applied to a whole Go file would match every
# string literal in the file, not just import paths -- Go import paths
# only appear inside `import (...)` blocks or a single-line `import "..."`.
_GO_IMPORT_BLOCK = re.compile(r"import\s*\((.*?)\)", re.DOTALL)
_GO_IMPORT_SINGLE = re.compile(r'^\s*import\s+"([\w./-]+)"', re.MULTILINE)
_GO_QUOTED = re.compile(r'"([\w./-]+)"')


def _extract_go_refs(text: str) -> list[str]:
    refs = [m.group(1) for m in _GO_IMPORT_SINGLE.finditer(text)]
    for block in _GO_IMPORT_BLOCK.finditer(text):
        refs.extend(_GO_QUOTED.findall(block.group(1)))
    return refs


def _extract_raw_refs(text: str, ext: str) -> list[str]:
    if ext == ".go":
        return _extract_go_refs(text)
    pattern = _IMPORT_PATTERNS.get(ext)
    if pattern is None:
        return []
    refs: list[str] = []
    for match in pattern.finditer(text):
        for group in match.groups():
            if group:
                refs.append(group)
                break
    return refs


def _normalize_segments(raw: str, ext: str) -> tuple[str, ...]:
    """Turn a raw import reference into path-like segments for suffix
    matching, e.g. `com.acme.util` -> ("com", "acme", "util");
    `../models/user` -> ("models", "user").
    """
    raw = raw.strip()
    if ext in (".java", ".cs", ".py"):
        raw = raw.replace(".", "/")
    if ext == ".rs":
        raw = raw.replace("::", "/")
    parts = [p for p in raw.split("/") if p not in ("", ".", "..")]
    return tuple(parts)


def _build_suffix_index(files: list[Path], root: Path) -> dict[tuple[str, ...], list[Path]]:
    """Index every file by every suffix of its (extension-stripped) path
    segments, so `("models", "user")` finds `src/app/models/user.py` and
    `("user",)` finds it too (as a lower-confidence fallback).

    Also indexes by *directory*-only suffixes (dropping the filename
    entirely), since some languages (Go, and often Java/Kotlin at the
    package level) import a package/directory rather than a specific
    file -- `"myrepo/internal/lib"` should be able to resolve to
    `internal/lib/thing.go` via its directory path, not just its stem.
    An ambiguous directory (more than one file in it) is still left
    unresolved by `_resolve`'s uniqueness check, same as file-stem
    ambiguity.
    """
    index: dict[tuple[str, ...], list[Path]] = defaultdict(list)
    for f in files:
        rel = f.relative_to(root)
        stem_parts = rel.with_suffix("").parts
        for k in range(1, len(stem_parts) + 1):
            index[stem_parts[-k:]].append(f)

        dir_parts = rel.parent.parts
        for k in range(1, len(dir_parts) + 1):
            index[dir_parts[-k:]].append(f)
    return index


def _resolve(
    segments: tuple[str, ...], index: dict[tuple[str, ...], list[Path]]
) -> Path | None:
    for k in range(len(segments), 0, -1):
        candidates = index.get(segments[-k:])
        if candidates and len(candidates) == 1:
            return candidates[0]
    return None


def build_import_graph(files: list[Path], root: Path) -> ImportGraph:
    """Build a best-effort import graph over `files` (already hard-
    exclusion-filtered, relative to `root`).
    """
    index = _build_suffix_index(files, root)
    in_degree: dict[Path, int] = {f: 0 for f in files}
    edges: dict[Path, set[Path]] = defaultdict(set)

    for f in files:
        ext = f.suffix
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for raw_ref in _extract_raw_refs(text, ext):
            segments = _normalize_segments(raw_ref, ext)
            if not segments:
                continue
            target = _resolve(segments, index)
            if target is not None and target != f and target not in edges[f]:
                edges[f].add(target)
                in_degree[target] += 1

    return ImportGraph(in_degree=in_degree, edges=dict(edges))
