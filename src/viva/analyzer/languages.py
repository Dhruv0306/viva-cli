"""FR6 language allowlist: file extension -> tree-sitter-language-pack
language key, and the matching `.scm` query file bundled in `queries/`.

The allowlist intentionally matches `ingest/stack.py`'s `_EXTENSION_STACK`
keys where both apply (05-repo-lifecycle-and-language-coverage.md §5.1) --
Phase 2 already enumerated the languages viva-cli claims to detect, and
Phase 3's AST support should cover that same set, not silently diverge
from it. Anything outside this allowlist (Kotlin, PHP, or any extension
not listed at all) falls back to line-window chunking, same as an
in-allowlist file that fails to parse.

`.tsx` uses the `tsx` grammar (TypeScript + JSX) rather than plain
`typescript`, since the latter's grammar doesn't parse JSX syntax -- but
reuses `typescript.scm`'s query, since `tsx` is a superset grammar with
the same `function_declaration`/`class_declaration`/`method_definition`
node types for the constructs this query captures.
"""
from __future__ import annotations

from pathlib import Path

# extension -> (tree-sitter-language-pack key, query filename stem)
_LANGUAGE_MAP: dict[str, tuple[str, str]] = {
    ".py": ("python", "python"),
    ".js": ("javascript", "javascript"),
    ".jsx": ("javascript", "javascript"),
    ".ts": ("typescript", "typescript"),
    ".tsx": ("tsx", "typescript"),
    ".java": ("java", "java"),
    ".go": ("go", "go"),
    ".rs": ("rust", "rust"),
    ".rb": ("ruby", "ruby"),
    ".cs": ("csharp", "csharp"),
    ".c": ("c", "c"),
    ".h": ("c", "c"),
    ".cpp": ("cpp", "cpp"),
    ".hpp": ("cpp", "cpp"),
    ".cc": ("cpp", "cpp"),
}

QUERIES_DIR = Path(__file__).parent / "queries"


def resolve_language(path: str) -> tuple[str, str] | None:
    """Return (tree-sitter language key, query file stem) for `path`'s
    extension, or None if it's outside the allowlist entirely."""
    suffix = Path(path).suffix.lower()
    return _LANGUAGE_MAP.get(suffix)


def load_query_source(query_stem: str) -> str:
    return (QUERIES_DIR / f"{query_stem}.scm").read_text(encoding="utf-8")
