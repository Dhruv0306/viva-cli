# System Design Reference — Part 6: CLI Contract & Project Profile Reduce Scaling

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. This part resolves two gaps from a second
> external design review: `viva resume`/`viva report` had no specified
> arg/output contract, and the map-reduce Project Profile generation had no
> defined behavior for repos whose per-module summaries themselves overflow
> the reduce step's context.

## 6.1 CLI Contract

The README's usage examples (`viva start`, `viva resume`, `viva report`)
were illustrative, not a spec. Concrete contract:

### `viva start <repo_url>`
| Flag | Default | Notes |
|---|---|---|
| `--branch <name>` | repo's default branch | Resolved to a commit SHA at `INGESTING` (design.md §8, `05-repo-lifecycle...` §5.2) |
| `--duration <minutes>` | `VIVA_DURATION_MINUTES` | Overrides env for this session only |
| `--session-name <label>` | none | Optional human-friendly label shown in `viva list` |

Prints the generated `session_id` to stdout immediately after `INGESTING`
starts, so it's captured even if the session is later interrupted before
completion — this is what makes `viva resume`/`viva report` reachable after
a crash.

### `viva resume <session_id>`
Resumes strictly from persisted state (`05-repo-lifecycle...` §5.3 — never
re-touches the remote repo). Errors, with exit code 3, if:
- the session doesn't exist, or
- the session is already `COMPLETE` (the error message points to
  `viva report <session_id>` instead).

### `viva report <session_id>`
| Flag | Default | Notes |
|---|---|---|
| `--format <md\|json>` | `md` | Per FR26/FR27; JSON is for downstream tooling/scripting, not a v1 requirement to look pretty |
| `--output <path>` | stdout | Writes to a file instead of printing |
| `--allow-partial` | off | Without this flag, reporting on a session that isn't `COMPLETE` errors (exit code 3) rather than silently showing an incomplete picture; with it, shows whatever's been finalized so far |

### `viva list`
Added because `resume`/`report` both require a `session_id` that otherwise
has no discovery path. Lists: `session_id`, repo slug, commit SHA (short),
status, created-at, duration used. No flags in v1 beyond `--status <state>`
to filter.

### `viva cleanup` (Phase 9, per `plan.md`)
| Flag | Default | Notes |
|---|---|---|
| `--older-than <days>` | `SESSION_RETENTION_DAYS` | Overrides the configured retention window for this run |
| `--all` | off | Full reset regardless of age |

### Exit codes (all commands)
`0` success · `1` unexpected/internal error · `2` invalid input (bad URL,
malformed flags) · `3` valid input but not actionable (session not found,
already complete, not yet complete without `--allow-partial`).

## 6.2 Project Profile: Hierarchical Reduce for Large Repos

The map-reduce Project Profile generation (design.md §1, FR7) as originally
described is a single flat reduce: summarize every sampled file, then
combine all per-file summaries into one project-level summary. That breaks
down for repos with enough modules that the per-module summaries alone
exceed the reduce step's usable context on a 7B-class local model.

**Decision: reduce is recursive, not flat, triggered only when needed.**

1. **Map** — every sampled file gets a bounded-length summary (target ~150
   tokens each), tagged with its module (from the directory-stratified
   sampling groups already computed in design.md §3).
2. **Level-1 reduce** — per-module: combine that module's file summaries
   into one module-level summary.
3. **Size check** — if the concatenation of all module-level summaries fits
   within `MAX_REDUCE_CONTEXT_TOKENS` (configurable, conservative default
   sized well under the model's real context window to leave room for the
   system prompt and output), reduce directly to the final project-level
   summary. This is the common case for small-to-medium repos and matches
   the originally described flat behavior.
4. **Level-2+ reduce (only if the size check fails)** — batch module
   summaries into groups of `MAP_REDUCE_BATCH_SIZE` (default 8), summarize
   each batch, then re-run the size check against the batch summaries.
   Repeat recursively until the summaries at some level fit in one reduce
   call. This is standard hierarchical/tree summarization, not a novel
   mechanism — the only project-specific part is that batching follows the
   existing module/directory boundaries rather than arbitrary chunking, so
   the resulting tree stays semantically meaningful (a batch summary reads
   as "these related modules do X," not an arbitrary grab-bag).

**Consequence for the Project Profile schema:** no change to the schema
itself (design.md §6) — `modules[]` still lists every module's own summary
at Level 1, regardless of how many higher reduce levels were needed to also
produce the single `architecture_summary` field. The recursion only affects
how that one top-level field gets built; per-module detail is unaffected
and always available at full Level-1 granularity for question grounding.

**New config:** `MAP_REDUCE_BATCH_SIZE` (default 8), `MAX_REDUCE_CONTEXT_TOKENS`
(default TBD — depends on the actual context window of whichever model is
configured via `LLM_MODEL`; should be computed as a fraction of the model's
known context size rather than hardcoded, since `LLM_MODEL` is itself
swappable).
