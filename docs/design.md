# Design — AI Project Viva Preparation Tool

> This is the canonical, build-facing design. For the full architectural iteration
> history (why this shape was chosen over two earlier versions), see
> `docs/system-design-full.md`.

## 1. Component Diagram

```
┌─────────────┐
│  CLI / UI   │  (Typer+Rich CLI first; FastAPI web UI later — same core underneath)
└──────┬──────┘
       │
┌──────▼───────────────────────────────────────────────────────────┐
│                        Orchestrator                               │
│   (owns the session state machine; every other component is a     │
│    service the orchestrator calls — no service calls another      │
│    directly)                                                      │
└──┬───────┬───────────┬────────────┬─────────────┬─────────────────┘
   │       │           │            │             │
┌──▼──┐ ┌──▼─────┐ ┌───▼────────┐ ┌─▼──────────┐ ┌▼─────────────┐
│Ingest│ │Analyzer│ │Indexer/RAG │ │QuestionGen │ │Evaluator      │
│      │ │        │ │(Chroma)    │ │(planner +  │ │(background    │
│      │ │        │ │            │ │ generator) │ │ worker)       │
└──────┘ └────────┘ └────────────┘ └────────────┘ └───────────────┘
                                                          │
                                                    ┌──────▼──────┐
                                                    │ReportBuilder │
                                                    └─────────────┘

              ┌──────────────────────────────┐
              │   Session Store (SQLite)      │  ← every component reads/writes
              │   Project Profile Store (JSON)│    through this, never in-memory only
              └──────────────────────────────┘
```

Rule: components never call each other directly — the Orchestrator mediates
everything through the Session Store. This keeps each stage independently
testable and makes crash-recovery close to free.

## 2. Session State Machine

```
INIT
  → INGESTING        (clone, filter, sample to file cap)
  → ANALYZING         (map-reduce summarization → Project Profile)
  → INDEXING          (tree-sitter chunk → embed → vector store)
  → PLANNING          (category coverage plan built from Project Profile)
  → IN_PROGRESS        ⇄ (ASKING → AWAITING_ANSWER → [EVAL queued async] → next)
  → TIME_EXPIRED | QUESTIONS_EXHAUSTED
  → FINALIZING_EVALS  (flush any evals not finished during the session)
  → SUMMARIZING
  → COMPLETE
```

Every transition is persisted immediately, not held only in memory.

## 3. Sampling Strategy (file cap enforcement)

Two-pass filtering, ordered:

1. **Hard exclusion** (doesn't count toward the cap): VCS/dependency/build
   directories, binaries, lockfiles, oversized generated files.
2. **Priority ranking** (if remaining > cap):
   - Import graph is built *before* ranking, from a cheap static scan
     (regex/AST import-statement parsing only, no LLM) over the full
     hard-exclusion-filtered set — not from the already-capped selection.
     This avoids a chicken-and-egg where centrality can't be computed
     because the files needed to compute it were already excluded.
   - Always-include tier: README, entry points, manifest files (outside the budget).
   - Import-graph centrality: files referenced by many others rank higher.
   - Directory-stratified allocation: budget split proportionally across
     top-level modules so one large module can't crowd out a smaller but
     architecturally important one.
   - Guaranteed test-file quota (default 10%) so the "testing strategy"
     question category doesn't silently degrade.

The Project Profile always records what was excluded/sampled, and the
Question Generator is not permitted to target excluded files.

## 4. Structured Output Strategy

1. Grammar/schema-constrained decoding at the inference layer (not
   prompt-only "please return JSON").
2. Pydantic schema validation on every structured LLM response, immediately
   on receipt.
3. Small-schema decomposition: multi-field outputs (e.g. evaluation) are
   split into a small classification call + a conditioned free-text call,
   since local-model JSON reliability degrades with field count. **This
   split has a concurrency consequence (see §7): the classification call
   is synchronous because follow-up generation depends on its result; only
   the free-text feedback call is deferred to the background.**
4. Repair loop: one re-prompt with the validation error attached → fallback
   heuristic extraction → mark `needs_review: true` and continue. A bad
   parse never blocks the session.

## 5. Ground-Truth Grounding Strategy

- Evaluator prompts are built from explicitly labeled, non-concatenated
  sections: `[QUESTION]`, `[GROUND_TRUTH_CODE_CONTEXT]`, `[USER_ANSWER]`.
- System prompt instructs: judge only against the provided code context; do
  not penalize omissions the code doesn't clearly demonstrate; do not import
  outside best-practice opinions unless the code contradicts them.
- Every `missed`/`did_wrong` item must cite a specific file/function; items
  without a citation are dropped before being surfaced to the user.
- Each pipeline stage (analysis / question-gen / evaluation) has its own
  system prompt with stage-specific grounding language.

## 6. Core Data Contracts

**Project Profile** (JSON, always-in-context, never retrieved)
```json
{
  "repo_url": "...",
  "files_analyzed": 500,
  "files_total": 823,
  "sampling_note": "prioritized by import centrality + directory coverage",
  "detected_stack": ["python", "fastapi", "postgres"],
  "architecture_summary": "...",
  "entry_points": ["src/main.py"],
  "modules": [
    {"path": "src/auth", "role": "authentication", "key_files": ["..."], "summary": "..."}
  ],
  "test_coverage_present": true,
  "excluded_notable": ["large generated migration files"]
}
```

**Question Plan Item**
```json
{
  "id": "q_03",
  "category": "error_handling",
  "target_module": "src/payments",
  "grounding_chunk_ids": ["chunk_182", "chunk_183"],
  "status": "pending | asked | answered | evaluated",
  "is_followup_of": null
}
```

**Q&A Record** (SQLite row)
```
session_id, question_id, question_text, grounding_chunk_ids,
answer_text, asked_at, answered_at, eval_status, eval_json
```

**Evaluation Record**
```json
{
  "question_id": "q_03",
  "classification": "correct | partial | incorrect | not_attempted",
  "summary": "...",
  "did_well": ["..."],
  "missed": [{"point": "...", "cited_file": "src/payments/handler.py:42"}],
  "did_wrong": [],
  "improvement": "..."
}
```

## 7. Timing Model

- The session clock starts at `IN_PROGRESS` and runs on an independent async
  timer. Only display time and answer time consume it — LLM generation and
  evaluation never do.
- **Evaluation is two calls, not one (see §4), with different scheduling:**
  the fast classification call (`correct | partial | incorrect | not_attempted`)
  runs synchronously right after an answer, because whether/how to generate
  a follow-up depends on it. The slower free-text feedback call
  (`did_well`/`missed`/`did_wrong`/`improvement`) is what gets deferred to
  the background during the next question's think-time.
- Before each question, the Orchestrator checks
  `remaining_time / avg_time_per_remaining_category` and collapses to one
  question per remaining uncovered category if time is short, favoring full
  coverage over depth.
- If time expires mid-answer, the in-flight answer is still captured and
  evaluated before the session moves to `FINALIZING_EVALS`.

## 8. Storage

- **SQLite** — sessions, questions, answers, evaluations. Single writer
  (Orchestrator), crash-safe, queryable for reporting.
- **Local vector store (Chroma)** — code chunks + metadata, one collection
  per repo+commit, keyed `{repo_slug}-{commit_sha}` (enables reuse without
  re-indexing an unchanged repo, and makes a new commit produce a fresh
  collection automatically rather than requiring manual invalidation).
- **Flat JSON** — Project Profile per session, loaded whole.

Full detail on keying, staleness, and resume behavior:
`docs/system-design/05-repo-lifecycle-and-language-coverage.md` §5.2–5.3.

## 8.1 Language Coverage & Fallback Chunking

Tree-sitter (§6, FR6) covers an explicit language allowlist. Files outside
it, or files that fail to parse, fall back to line-window chunking (60
lines, 15-line overlap) rather than being dropped — every chunk carries a
`parse_method: "ast" | "line_window"` tag, and `line_window` chunks are
deprioritized for structure-dependent question categories. Full detail:
`docs/system-design/05-repo-lifecycle-and-language-coverage.md` §5.1.

## 8.2 Data Lifecycle (NFR7)

| Data | Lifetime |
|---|---|
| Raw cloned repo source | Deleted immediately after `INDEXING` completes |
| SQLite records + Project Profile | Kept until `SESSION_RETENTION_DAYS` expires, or indefinitely if unset |
| Chroma collections | Same retention window; reusable within it by repo+commit key (§8) |

`viva cleanup` (Phase 9) enforces this on demand. Full rationale:
`docs/system-design/05-repo-lifecycle-and-language-coverage.md` §5.4.

## 9. Failure Handling

| Case | Handling |
|---|---|
| Repo exceeds file cap | Stratified sampling (§3), logged in Project Profile |
| Private repo | `.env` token; explicit auth-failure error |
| No detectable stack | Analyzer falls back to generic "document what exists" mode |
| Malformed LLM JSON | 3-layer fallback (§4); worst case `needs_review: true` |
| Evaluation unfinished at session end | Completed in `FINALIZING_EVALS`, never dropped |
| Crash mid-session | State machine + persisted store → resumable |
| Ungrounded model criticism | Dropped if no file citation present (§5) |
| Timer expires mid-answer | Answer captured and evaluated, not discarded |

## 10. Interfaces & Extensibility

- `LLMClient` and `EmbeddingClient` are thin interfaces; Ollama is the v1
  implementation. Nothing in the pipeline should import Ollama directly.
- CLI and future web UI both sit on top of the same Orchestrator — no
  pipeline logic lives in the interface layer.

## 11. Non-Functional Notes

- Single active session at a time (v1); no concurrency control needed
  beyond SQLite defaults.
- Config via `.env`: `VIVA_DURATION_MINUTES`, `MAX_QUESTIONS`,
  `TOP_K_RETRIEVAL`, `MAX_FILES` (default 500), `TEST_FILE_QUOTA_PCT`
  (default 10), `MAX_FOLLOWUP_DEPTH` (default 1), `SESSION_RETENTION_DAYS`
  (default 7), `LLM_MODEL`, `EMBEDDING_MODEL`, `TEMPERATURE`.
- Cloned repository code is never executed — static parsing only (NFR6).
- Cloned repos and per-session indices follow a defined cleanup/retention
  policy rather than accumulating unbounded (NFR7, §8.2).
- Timer is a live, continuously updating countdown during `IN_PROGRESS`
  (FR17) — never hidden or periodic-only, since it already excludes LLM
  latency and so reflects genuine remaining answer time.
