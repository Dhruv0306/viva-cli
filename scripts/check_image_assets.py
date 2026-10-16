"""Check that the built Docker image's installed package is complete.

Runs INSIDE the image (the CI job pipes it to `python -` via
`docker run -i`), with the repo's `src/` mounted read-only at the path
given as argv[1]. Phase 24 (docs/system-design/29-phase-24-docker-ci-job-
design.md).

The image installs the project with a plain `pip install .` (a real
wheel build), so this is the same bug class `release.yml` guards against
for the standalone wheel (§27.8): non-.py files that setuptools drops
unless they are in `package-data`.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

STATIC_FILES = ("index.html", "style.css", "app.js", "voice-worklet.js", "favicon.svg")


def find_problems(installed: Path, source_queries: Path) -> list[str]:
    problems: list[str] = []
    if "site-packages" not in installed.parts and "dist-packages" not in installed.parts:
        problems.append(
            f"viva is imported from {installed}, not from site-packages "
            "(the image must run a real install, not the source tree)"
        )
    for name in STATIC_FILES:
        if not (installed / "web" / "static" / name).is_file():
            problems.append(f"missing viva/web/static/{name} in the installed package")
    # Checked against the source tree's own list, so a newly added
    # language's query file is covered without editing this script.
    for scm in sorted(source_queries.glob("*.scm")):
        if not (installed / "analyzer" / "queries" / scm.name).is_file():
            problems.append(f"missing viva/analyzer/queries/{scm.name} in the installed package")
    if not any(source_queries.glob("*.scm")):
        problems.append(f"found no .scm files in {source_queries}; the check would pass vacuously")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python - <source-queries-dir>", file=sys.stderr)
        return 2
    spec = importlib.util.find_spec("viva")
    if spec is None or spec.origin is None:
        print("FAIL: viva is not importable in the image", file=sys.stderr)
        return 1
    problems = find_problems(Path(spec.origin).parent, Path(argv[1]))
    if shutil.which("git") is None:
        problems.append("git is not installed in the image (GitPython needs the binary)")
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if not problems:
        print("image assets: OK")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
