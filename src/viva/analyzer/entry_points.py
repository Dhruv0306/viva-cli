"""Entry-point detection for the Project Profile's `entry_points` field
(docs/design.md §6).

Filename-based heuristic, same "cheap, no code understanding required"
spirit as `ingest/stack.py`'s manifest/extension detection -- true
program-entry-point detection (following `if __name__ == "__main__"`,
`func main()`, etc.) would need per-language AST analysis for marginal
benefit over just recognizing the handful of filenames convention
already gives us across the FR6 language allowlist.
"""
from __future__ import annotations

from pathlib import Path

from viva.ingest.models import SampledFile

_ENTRY_POINT_BASENAMES: set[str] = {
    "main.py",
    "__main__.py",
    "app.py",
    "manage.py",
    "wsgi.py",
    "asgi.py",
    "index.js",
    "index.ts",
    "server.js",
    "server.ts",
    "main.go",
    "main.rs",
    "main.java",
    "application.java",
    "program.cs",
    "startup.cs",
}


def detect_entry_points(sampled_files: list[SampledFile], detected_stack: list[str]) -> list[str]:
    """Return sampled-file paths whose basename matches a well-known
    entry-point convention. `detected_stack` is accepted for interface
    symmetry with `ingest/stack.py`'s detection pass -- matching is
    intentionally stack-agnostic (the basename set is already
    unambiguous enough on its own) so a repo with an undetected/unusual
    stack still gets entry points recognized rather than none at all.
    """
    del detected_stack  # see docstring: not needed for matching itself

    matches = {
        f.path for f in sampled_files if Path(f.path).name.lower() in _ENTRY_POINT_BASENAMES
    }
    return sorted(matches)
