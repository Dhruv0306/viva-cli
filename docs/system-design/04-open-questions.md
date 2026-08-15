# System Design Reference — Part 4: Open Questions Before Build

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. These are product/design questions not yet
> resolved — unlike Part 1, which documents decisions already made.

1. **Follow-up depth** — how many follow-ups deep can one category go before forcing a move-on (to guarantee coverage breadth)? Needs a concrete configured max (e.g. `MAX_FOLLOWUP_DEPTH`).
2. **Blank/"I don't know" answers** — should these be logged as `did_wrong` or as a neutral `not_attempted` classification? Recommendation carried into `03-final-architecture.md` §3.3: use a distinct `not_attempted` value — conflating "wrong" with "skipped" would muddy the summary and unfairly penalize the user for honesty about a knowledge gap versus a factually incorrect answer.
3. **Timer visibility** — should the CLI show a live countdown, or hide remaining time to reduce exam anxiety? This is a product/UX decision, not an architectural one — the pipeline (`../design.md` §7) supports either without changes.
