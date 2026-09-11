"""Regression test for a real bug found while setting up the release
workflow (v0.1.0): `src/viva/__init__.py`'s `__version__` and
`pyproject.toml`'s `[project] version` are two separate hardcoded
strings with nothing tying them together -- easy for a version bump to
update one and silently miss the other, and nothing else in the suite
would have caught it, since `viva --version` itself has no test
coverage either.

Parses `pyproject.toml` directly with the stdlib `tomllib` (3.11+,
matching this project's `requires-python`) rather than hardcoding a
second copy of the expected version string here, which would just
recreate the same two-sources-of-truth problem inside the test itself.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from typer.testing import CliRunner

from viva import __version__
from viva.cli import app

runner = CliRunner()


def _pyproject_version() -> str:
    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]


def test_dunder_version_matches_pyproject_toml():
    assert __version__ == _pyproject_version()


def test_cli_version_flag_reports_the_same_version():
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.output
