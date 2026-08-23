"""Tests for viva.ingest.filters (Pass A hard exclusion).

Uses synthetic trees built directly under pytest's tmp_path rather than
checked-in fixtures: exclusion-dir names like `.git` and `node_modules`
are awkward to check into this repo's own tree (git treats a nested
`.git` directory specially), and precise control over exactly which
directories/files exist makes the exclusion-count assertions exact
rather than approximate.
"""
from __future__ import annotations

from pathlib import Path

from viva.ingest.filters import (
    MAX_FILE_SIZE_BYTES,
    walk_and_hard_exclude,
)


def _write(root: Path, rel_path: str, content: bytes = b"hello") -> Path:
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_keeps_ordinary_source_files(tmp_path: Path) -> None:
    _write(tmp_path, "src/app/main.py", b"print('hi')\n")
    _write(tmp_path, "README.md", b"# Hello\n")

    outcome = walk_and_hard_exclude(tmp_path)

    kept_rel = {p.relative_to(tmp_path).as_posix() for p in outcome.kept}
    assert kept_rel == {"src/app/main.py", "README.md"}
    assert outcome.stats.excluded_dirs == 0
    assert outcome.stats.excluded_binary == 0
    assert outcome.stats.excluded_lockfile == 0
    assert outcome.stats.excluded_oversized == 0


def test_prunes_excluded_directories_without_descending(tmp_path: Path) -> None:
    _write(tmp_path, "src/main.py", b"print(1)\n")
    _write(tmp_path, "node_modules/leftpad/index.js", b"module.exports = 1;\n")
    _write(tmp_path, "node_modules/leftpad/nested/deep.js", b"x\n")
    _write(tmp_path, ".git/HEAD", b"ref: refs/heads/main\n")
    _write(tmp_path, "__pycache__/main.cpython-311.pyc", b"\x00\x01")
    _write(tmp_path, "vendor/lib/thing.go", b"package lib\n")

    outcome = walk_and_hard_exclude(tmp_path)

    kept_rel = {p.relative_to(tmp_path).as_posix() for p in outcome.kept}
    assert kept_rel == {"src/main.py"}
    # 4 excluded dirs at their point of discovery: node_modules, .git,
    # __pycache__, vendor -- their nested contents are never walked, so
    # the count reflects pruned directories, not files inside them.
    assert outcome.stats.excluded_dirs == 4


def test_nested_excluded_directory_is_pruned(tmp_path: Path) -> None:
    _write(tmp_path, "frontend/node_modules/pkg/index.js", b"x\n")
    _write(tmp_path, "frontend/src/app.js", b"console.log(1);\n")

    outcome = walk_and_hard_exclude(tmp_path)

    kept_rel = {p.relative_to(tmp_path).as_posix() for p in outcome.kept}
    assert kept_rel == {"frontend/src/app.js"}
    assert outcome.stats.excluded_dirs == 1


def test_egg_info_directory_excluded_by_suffix(tmp_path: Path) -> None:
    _write(tmp_path, "src/main.py", b"x = 1\n")
    _write(tmp_path, "my_pkg.egg-info/PKG-INFO", b"Name: my_pkg\n")

    outcome = walk_and_hard_exclude(tmp_path)

    kept_rel = {p.relative_to(tmp_path).as_posix() for p in outcome.kept}
    assert kept_rel == {"src/main.py"}


def test_excludes_known_lockfiles(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", b'{"name": "x"}')
    _write(tmp_path, "package-lock.json", b"{}")
    _write(tmp_path, "poetry.lock", b"# generated\n")
    _write(tmp_path, "Cargo.lock", b"# generated\n")

    outcome = walk_and_hard_exclude(tmp_path)

    kept_rel = {p.relative_to(tmp_path).as_posix() for p in outcome.kept}
    assert kept_rel == {"package.json"}
    assert outcome.stats.excluded_lockfile == 3


def test_excludes_oversized_files(tmp_path: Path) -> None:
    _write(tmp_path, "small.py", b"x = 1\n")
    _write(tmp_path, "big_data.py", b"x" * (MAX_FILE_SIZE_BYTES + 1))

    outcome = walk_and_hard_exclude(tmp_path)

    kept_rel = {p.relative_to(tmp_path).as_posix() for p in outcome.kept}
    assert kept_rel == {"small.py"}
    assert outcome.stats.excluded_oversized == 1


def test_excludes_binary_files_by_content_sniff(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", b"x = 1\n")
    # A binary file with a source-like extension should still be caught --
    # this is why detection is content-based (null byte sniff), not just
    # an extension denylist.
    _write(tmp_path, "sneaky.py", b"\x00\x01\x02binary-disguised-as-python")

    outcome = walk_and_hard_exclude(tmp_path)

    kept_rel = {p.relative_to(tmp_path).as_posix() for p in outcome.kept}
    assert kept_rel == {"app.py"}
    assert outcome.stats.excluded_binary == 1


def test_unreadable_file_counts_as_excluded_binary(tmp_path: Path) -> None:
    good = _write(tmp_path, "app.py", b"x = 1\n")
    broken_link = tmp_path / "broken_link.py"
    broken_link.symlink_to(tmp_path / "does_not_exist.py")

    outcome = walk_and_hard_exclude(tmp_path)

    assert outcome.kept == [good]
    assert outcome.stats.excluded_binary == 1


def test_empty_tree_returns_no_files(tmp_path: Path) -> None:
    outcome = walk_and_hard_exclude(tmp_path)

    assert outcome.kept == []
    assert outcome.stats.excluded_dirs == 0
