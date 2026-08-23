"""Ingest component (docs/plan.md Phase 2, docs/design.md §1 "Ingest").

Clones a repo, applies hard exclusion + priority sampling, and detects the
primary tech stack, producing an `IngestResult` that feeds the Project
Profile fields the Analyzer (Phase 3) doesn't own (`files_analyzed`,
`files_total`, `sampling_note`, `detected_stack`, `excluded_notable`).

This package is built up incrementally over Phase 2's patch series; the
public `ingest_repo()` entrypoint lands once cloning is wired in.
"""
