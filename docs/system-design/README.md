# System Design Reference

This folder is the detailed reference record behind `../design.md` (the
canonical, build-facing design). Read `../design.md` first if you're
building; come here when you need the reasoning behind a decision, or the
history of what was tried and rejected along the way.

- **[01-resolved-decisions.md](01-resolved-decisions.md)** — the four
  design decisions with the most alternatives considered: file-sampling
  strategy, structured-output reliability approach, and ground-truth
  grounding for evaluation, including the post-validator refinements to
  each.
- **[02-iteration-log.md](02-iteration-log.md)** — the four architecture
  iterations (naive → state machine → component boundaries → post-review
  fixes), each with the specific problems found that forced the next
  revision. Read this to understand *why* the design has the shape it does,
  not just what the shape is.
- **[03-final-architecture.md](03-final-architecture.md)** — the fully
  detailed version of the architecture: component diagram, state machine,
  data contracts, timing model, storage, failure handling, interfaces, and
  non-functional notes. Kept consistent with `../design.md`; that file is
  the trimmed, build-facing version of this one.
- **[04-open-questions.md](04-open-questions.md)** — product/design
  questions not yet resolved, as distinct from decisions already made in
  Part 1.
- **[05-repo-lifecycle-and-language-coverage.md](05-repo-lifecycle-and-language-coverage.md)**
  — tree-sitter fallback chunking for unsupported/unparseable files, Chroma
  collection keying and staleness, resume-vs-changed-repo behavior, and the
  concrete NFR7 retention policy. Added in response to external design
  review.
- **[06-cli-contract-and-profile-scaling.md](06-cli-contract-and-profile-scaling.md)**
  — the full `viva` CLI command contract (args, flags, exit codes,
  including the previously-undocumented `viva list`), and the recursive
  hierarchical-reduce fallback for Project Profile generation on repos
  with many modules. Added in response to a second external design review.
- **[08-phase-3-analyzer-design.md](08-phase-3-analyzer-design.md)** — the
  Phase 3 Analyzer implementation design: tree-sitter dependency choice,
  the query-per-language extraction mechanism, the `ProjectProfile`
  unification decision, and the golden-repo fixture strategy for the
  hierarchical-reduce path.

See also: `../requirements.md` (functional/non-functional requirements)
and `../plan.md` (phased build plan).
