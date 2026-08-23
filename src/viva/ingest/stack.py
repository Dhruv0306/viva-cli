"""FR5: primary technology stack detection.

Manifest-first (most reliable signal), a framework-hint pass second, and
extension-distribution as a fallback/secondary signal for repos with no
recognizable manifest -- see design.md §9's "repo with no detectable
stack" row. The actual fallback *behavior* (a generic "document what
exists" analysis mode) belongs to the Analyzer (Phase 3); this module's
job ends at producing the `detected_stack` list itself.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

_MANIFEST_STACK: dict[str, str] = {
    "package.json": "node",
    "requirements.txt": "python",
    "pyproject.toml": "python",
    "setup.py": "python",
    "pipfile": "python",
    "pom.xml": "java",
    "build.gradle": "java",
    "build.gradle.kts": "kotlin",
    "go.mod": "go",
    "cargo.toml": "rust",
    "gemfile": "ruby",
    "composer.json": "php",
}

# Cheap secondary signal checked after the manifest-based stack: presence
# of a well-known framework marker file. Only added if not already
# implied by the manifest pass, so e.g. a plain "python" + "django" repo
# reports both rather than just one.
_FRAMEWORK_HINTS: dict[str, str] = {
    "manage.py": "django",
    "next.config.js": "next.js",
    "next.config.ts": "next.js",
}

_EXTENSION_STACK: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".kt": "kotlin",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".cs": "csharp",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
}

# Below this fraction of the extension-recognized file count, an
# extension-based stack guess is too thin to report -- avoids a single
# stray `.rb` script in an otherwise-Python repo showing up as "ruby".
_EXTENSION_STACK_MIN_SHARE = 0.05


def detect_stack(files: list[Path], root: Path) -> list[str]:
    """Detect the primary stack(s) present in a hard-exclusion-filtered
    file set. `root` is accepted for interface symmetry with the other
    Ingest passes (all of which key off repo-relative structure); this
    pass only looks at file names and extensions, not path position.
    """
    stack: list[str] = []
    seen: set[str] = set()

    for f in files:
        hit = _MANIFEST_STACK.get(f.name.lower())
        if hit and hit not in seen:
            stack.append(hit)
            seen.add(hit)

    for f in files:
        hint = _FRAMEWORK_HINTS.get(f.name.lower())
        if hint and hint not in seen:
            stack.append(hint)
            seen.add(hint)

    if not stack:
        stack.extend(_detect_from_extensions(files))

    return stack


def _detect_from_extensions(files: list[Path]) -> list[str]:
    counts: Counter[str] = Counter()
    for f in files:
        lang = _EXTENSION_STACK.get(f.suffix.lower())
        if lang:
            counts[lang] += 1

    total = sum(counts.values())
    if total == 0:
        return []

    return [
        lang
        for lang, count in counts.most_common()
        if count / total >= _EXTENSION_STACK_MIN_SHARE
    ]
