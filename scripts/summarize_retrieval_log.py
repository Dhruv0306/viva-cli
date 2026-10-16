"""Summarize retrieval distances per question category from viva logs.

Phase 26 (docs/system-design/31-phase-26-followups-design.md §31.5), for
the Phase 16 follow-up in docs/plan.md: is MAX_RETRIEVAL_DISTANCE=0.85 right
for `error_handling` and `testing_strategy`, which no real data covered?

Reads the `Retrieval for category=...` INFO line every question already
logs (questiongen/retrieval.py), from the files given or from stdin:

    python scripts/summarize_retrieval_log.py logs/*.log
    Get-Content logs\\viva.log | python scripts/summarize_retrieval_log.py

and prints, per category, how many retrievals ran, how many ended with no
usable result (the cause of SKIPPED_NO_GROUNDING), and the spread of the
best and worst distance among the results kept. It reports; it does not
change any setting. Standard library only.
"""
from __future__ import annotations

import re
import statistics
import sys
from dataclasses import dataclass, field

LINE = re.compile(
    r"Retrieval for category=(?P<category>\S+) .*?: "
    r"(?P<fetched>\d+) candidate\(s\) fetched, "
    r"(?P<after_test>\d+) after test-path filter, "
    r"(?P<kept>\d+) after relevance filter "
    r"\(distances: min=(?P<min>\S+) max=(?P<max>[^\s)]+)\)"
)


@dataclass
class CategoryStats:
    retrievals: int = 0
    empty: int = 0
    emptied_by_distance_filter: int = 0
    best: list[float] = field(default_factory=list)   # per retrieval: closest kept chunk
    worst: list[float] = field(default_factory=list)  # per retrieval: farthest kept chunk


def parse(lines) -> dict[str, CategoryStats]:
    stats: dict[str, CategoryStats] = {}
    for line in lines:
        match = LINE.search(line)
        if match is None:
            continue
        entry = stats.setdefault(match["category"], CategoryStats())
        entry.retrievals += 1
        if int(match["kept"]) == 0:
            entry.empty += 1
            if int(match["after_test"]) > 0:
                # Candidates existed but none was close enough: the
                # distance filter, not an empty index, ended this question.
                entry.emptied_by_distance_filter += 1
        else:
            entry.best.append(float(match["min"]))
            entry.worst.append(float(match["max"]))
    return stats


def render(stats: dict[str, CategoryStats]) -> str:
    if not stats:
        return "No 'Retrieval for category=...' lines found."
    header = (
        f"{'category':<22}{'n':>4}{'empty':>7}{'by-dist':>9}"
        f"{'best min':>10}{'best med':>10}{'worst med':>11}{'worst max':>11}"
    )
    rows = [header, "-" * len(header)]
    for category in sorted(stats):
        s = stats[category]
        if s.best:
            cells = (
                f"{min(s.best):>10.3f}{statistics.median(s.best):>10.3f}"
                f"{statistics.median(s.worst):>11.3f}{max(s.worst):>11.3f}"
            )
        else:
            cells = f"{'n/a':>10}{'n/a':>10}{'n/a':>11}{'n/a':>11}"
        rows.append(
            f"{category:<22}{s.retrievals:>4}{s.empty:>7}{s.emptied_by_distance_filter:>9}{cells}"
        )
    rows.append("")
    rows.append("empty: retrievals with no chunk kept (these skip the question).")
    rows.append("by-dist: of those, ones where candidates existed but all were too far.")
    rows.append("best/worst: the closest and farthest kept chunk per retrieval (lower is closer).")
    return "\n".join(rows)


def main(argv: list[str]) -> int:
    if argv:
        lines: list[str] = []
        for path in argv:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines.extend(handle)
    else:
        lines = sys.stdin.read().splitlines()
    print(render(parse(lines)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
