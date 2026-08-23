"""Pass A: hard exclusion (docs/design.md §3, 01-resolved-decisions.md §1.1).

Runs before any ranking or capping and never counts against MAX_FILES --
these are files that should never be considered as source material for
analysis, regardless of the file budget. Directory pruning happens during
the walk itself (not filtered after the fact) so an excluded subtree like
`node_modules` is never even descended into, which matters for repos where
that subtree could otherwise dwarf the rest of the codebase.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from viva.ingest.models import ExclusionStats

# VCS/dependency/build/vendor directories -- matched by directory *name*
# anywhere in the tree, not just at the root, since a nested
# `frontend/node_modules` is just as excludable as a root-level one.
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "venv",
        ".venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        "target",
        "out",
        ".idea",
        ".vscode",
        ".tox",
        "vendor",
        "bower_components",
    }
)

# Known lockfiles -- machine-generated, not hand-written, and not useful
# signal for a code-understanding viva.
EXCLUDED_LOCKFILE_NAMES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "Gemfile.lock",
        "composer.lock",
        "go.sum",
        "mix.lock",
    }
)

# design.md §3: "Files over a size threshold (e.g. 200KB) -- usually
# generated/data files, not hand-written logic." Not exposed as a config
# tunable -- FR28's tunable list doesn't call this out, and it's a
# filtering heuristic rather than a session-affecting knob.
MAX_FILE_SIZE_BYTES = 200 * 1024

# Sniff this many leading bytes for a null byte to classify a file as
# binary -- cheap, standard heuristic; avoids relying on extension lists
# alone, which miss binaries with source-like extensions.
_BINARY_SNIFF_BYTES = 8192


def _is_excluded_dir(dir_name: str) -> bool:
    return dir_name in EXCLUDED_DIR_NAMES or dir_name.endswith(".egg-info")


def _is_lockfile(file_name: str) -> bool:
    return file_name in EXCLUDED_LOCKFILE_NAMES


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            chunk = fh.read(_BINARY_SNIFF_BYTES)
    except OSError:
        # Unreadable file (broken symlink, permissions) -- exclude rather
        # than let one bad file crash the whole ingest walk.
        return True
    return b"\x00" in chunk


@dataclass
class FilterOutcome:
    kept: list[Path]
    stats: ExclusionStats


def walk_and_hard_exclude(root: Path) -> FilterOutcome:
    """Walk `root` and apply Pass A hard exclusion.

    Returns surviving file paths (absolute) plus exclusion stats for
    Project Profile transparency (FR4).
    """
    kept: list[Path] = []
    stats = ExclusionStats()

    for dirpath_str, dirnames, filenames in os.walk(root):
        dirpath = Path(dirpath_str)

        survivors = []
        for d in dirnames:
            if _is_excluded_dir(d):
                stats.excluded_dirs += 1
            else:
                survivors.append(d)
        dirnames[:] = survivors

        for fname in filenames:
            fpath = dirpath / fname

            if _is_lockfile(fname):
                stats.excluded_lockfile += 1
                continue

            try:
                size = fpath.stat().st_size
            except OSError:
                stats.excluded_binary += 1
                continue

            if size > MAX_FILE_SIZE_BYTES:
                stats.excluded_oversized += 1
                continue

            if _is_binary(fpath):
                stats.excluded_binary += 1
                continue

            kept.append(fpath)

    return FilterOutcome(kept=kept, stats=stats)
