# System Design Reference — Part 5: Repo Lifecycle & Language Coverage

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. This part resolves gaps identified in an
> external design review after Iteration 4: tree-sitter fallback behavior,
> Chroma collection keying/staleness, resume-vs-stale-index, and the
> concrete NFR7 retention policy.

## 5.1 Tree-sitter Language Coverage & Fallback Chunking

**Decision:** maintain an explicit supported-language allowlist (grammars
bundled at build time): Python, JavaScript/TypeScript, Java, Go, Rust, C/C++,
Ruby, C#. Any file outside this list, or any file inside it that fails to
parse (syntax errors, unsupported dialect), does **not** get dropped from
analysis — it falls back to a simpler chunking strategy so it's still
searchable and citable, just with lower structural confidence:

- **Fallback chunking:** fixed-size line-window chunks (default 60 lines,
  15-line overlap) instead of AST-node chunks.
- **Metadata tag:** every chunk carries `parse_method: "ast" | "line_window"`.
  This is what makes the degradation visible downstream rather than silent.
- **Question Generator consequence:** chunks tagged `line_window` are
  deprioritized for "explain this function's design" style questions (which
  need real symbol boundaries to be fair) but remain eligible for
  higher-level questions (e.g. "what does this config control") where exact
  AST structure isn't required.
- **Per-file parse errors are logged** per NFR8, and surfaced in the Project
  Profile's `excluded_notable`/sampling-note style transparency section so
  the user can see "12 files fell back to line-window chunking" rather than
  silently getting shallower coverage.

This directly closes the gap the reviewer flagged: there's now a defined
answer for "AST parse fails or language unsupported," not just a logging
requirement with no strategy behind it.

## 5.2 Chroma Collection Keying & Staleness

**Decision:** the vector-store collection key is `{repo_slug}-{commit_sha}`,
where `commit_sha` is the exact commit checked out during `INGESTING`
(short SHA, e.g. first 12 chars) — **not** a content hash of the sampled
500 files, and not the branch name.

Rationale: commit SHA is what the repo's own version control already uses
to mean "this exact state of the code," so reusing it avoids inventing a
second staleness-detection mechanism. Consequences:

- Re-running `viva start` on a repo that has new commits since the last run
  produces a **new** collection under a new key automatically — no manual
  invalidation logic needed, and no risk of silently evaluating a viva
  against outdated code.
- The commit SHA is pinned into the session record at `INGESTING` and never
  re-resolved afterward. A session's "notion of the repo" is fixed for its
  entire lifetime, including resume (§5.3).
- Old collections from earlier commits of the same repo become orphaned and
  fall under the retention policy in §5.4, not under any special
  "superseded" handling — simpler to have one cleanup mechanism than two.

## 5.3 Resume vs. a Repo That Has Since Changed

**Decision:** resume never re-checks the remote repo's current state.
`resume-session <id>` operates entirely against the session's already-pinned
`commit_sha`, the persisted Project Profile, and the existing Chroma
collection keyed to that SHA — all of which are independent of whatever the
GitHub repo looks like *now*.

If the user has since pushed new commits and wants the viva to reflect them,
that is explicitly a **new session** (`viva start`, which pins a fresh SHA
and creates a fresh collection), not a resume. This keeps "resume" simple
and correct by construction rather than needing to detect and reconcile
drift — which also means resume has no dependency on network access at all
once a session has passed `INDEXING`.

## 5.4 NFR7 Retention Policy (concrete)

Three data categories, three different lifetimes:

| Data | Lifetime | Rationale |
|---|---|---|
| Raw cloned repo source | Deleted immediately once `INDEXING` completes | Not needed afterward — the Project Profile and indexed chunks are the only things later stages read. Removing it early also shrinks the window in which cloned code sits on disk at all (reinforces NFR6's "never execute" posture by minimizing exposure, not just avoiding execution). |
| SQLite session/Q&A/eval records + Project Profile JSON | Kept indefinitely by default, configurable via `SESSION_RETENTION_DAYS` | Small (text-only), and needed for `viva report <session-id>` after the fact. |
| Chroma collections | Kept for reuse (§5.2) but subject to `SESSION_RETENTION_DAYS` cleanup, or removable on-demand via `viva cleanup` | These can grow unbounded across many repos/commits over time and are the only category here with real disk-size risk. |

`viva cleanup` (Phase 9, per `plan.md`) removes anything past its retention
window, plus supports `--all` for a full reset. This is the concrete
mechanism NFR7 previously referenced only abstractly.
