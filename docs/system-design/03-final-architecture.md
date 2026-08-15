# System Design Reference — Part 3: Final Architecture (Detailed)

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. This is the fully detailed version of what
> `../design.md` presents as the canonical, build-facing design — the two
> should stay consistent; if they ever diverge, `../design.md` is source of
> truth for what to build, and this file should be updated to match.

## 3.1 Component Diagram

```
┌─────────────┐
│  CLI / UI   │  (Typer+Rich CLI first; FastAPI web UI later — same core underneath)
└──────┬──────┘
       │
┌──────▼───────────────────────────────────────────────────────────┐
│                        Orchestrator                               │
│   (owns the session state machine; everything else is a service   │
│    the orchestrator calls — no service calls another directly)    │
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

Key rule: **components never call each other directly** — the Orchestrator mediates everything through the Session Store. This is what makes each component independently testable and lets you later run Evaluator as an actual background process/thread without redesigning anything.

## 3.2 Session State Machine

```
INIT
  → INGESTING        (clone, filter, sample to 500 files)
  → ANALYZING         (map-reduce summarization → Project Profile)
  → INDEXING          (tree-sitter chunk → embed → Chroma)
  → PLANNING          (category coverage plan built from Project Profile)
  → IN_PROGRESS        ⇄ (ASKING → AWAITING_ANSWER → [EVAL queued async] → next)
  → TIME_EXPIRED | QUESTIONS_EXHAUSTED
  → FINALIZING_EVALS  (flush any evals not finished during the session)
  → SUMMARIZING
  → COMPLETE
```

Every transition is persisted to SQLite immediately, not just held in memory — this is what makes crash-mid-viva recoverable (`resume-session <id>` becomes possible almost for free).

## 3.3 Core Data Contracts

**Project Profile** (JSON, always-in-context, not retrieved)
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
    {"path": "src/auth", "role": "authentication", "key_files": [...], "summary": "..."}
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

Schemas are deliberately flat and small per-object — this is what makes constrained decoding (Part 1, §1.2) reliable on a local model.

## 3.4 Timing Model

- Session clock starts at `IN_PROGRESS`, counts down independently (async timer task), and is **only consumed by**: reading the question (assume instant/display), and the user's answer time. LLM generation and evaluation time do **not** count against the user's 30 minutes.
- **Evaluation scheduling (post-validator refinement):** the fast classification call (`correct | partial | incorrect | not_attempted`) runs synchronously right after an answer, because follow-up generation depends on it. The slower free-text feedback call (`did_well`/`missed`/`did_wrong`/`improvement`) is what's deferred to the background during the next question's think-time.
- Before generating each question, the Orchestrator checks: `remaining_time / avg_time_per_remaining_category`. If time is short, it collapses to one question per remaining uncovered category instead of allowing follow-ups, so shallow-but-full-coverage beats deep-but-partial when the clock is tight.
- Hard stop: if the timer hits zero mid-answer, the current answer is still captured and evaluated (don't discard a mid-flight answer), then the session moves straight to `FINALIZING_EVALS`.

## 3.5 Storage

- **SQLite** — sessions, questions, answers, evaluations. One writer (Orchestrator), simple, crash-safe, queryable for the report phase.
- **ChromaDB** — code chunks (function/class granularity from tree-sitter) + metadata (filepath, symbol name, module role). One collection per session, named by repo hash — allows re-running a viva on the same repo without re-indexing if the repo hasn't changed.
- **Flat JSON file** — Project Profile per session. Small, always loaded whole, never queried piecemeal.

## 3.6 Failure Handling / Edge Cases Explicitly Designed For

| Case | Handling |
|---|---|
| Repo >500 files | Stratified sampling per Part 1 §1.1, logged in Project Profile |
| Private repo | `.env` GitHub token, clear error if auth fails |
| Repo with no detectable stack (e.g. pure config repo) | Analyzer falls back to a generic "document what exists" mode rather than failing |
| LLM returns malformed JSON | 3-layer fallback per Part 1 §1.2, worst case `needs_review: true`, session continues |
| Evaluation not finished when session ends | Deferred to `FINALIZING_EVALS` before summary, not blocking the live viva |
| Crash mid-session | State machine + SQLite persistence → `resume-session` support |
| Model hallucinates a criticism with no grounding | Evaluator drops any `missed`/`did_wrong` item lacking a file citation (Part 1 §1.3) |
| Timer expires mid-answer | Answer still captured and evaluated, session proceeds to finalize, not discarded |

## 3.7 Interfaces & Extensibility

- `LLMClient` and `EmbeddingClient` are thin interfaces; Ollama is the v1 implementation. Nothing in the pipeline should import Ollama directly.
- CLI and future web UI both sit on top of the same Orchestrator — no pipeline logic lives in the interface layer.

## 3.8 Non-Functional Notes

- Single-user, single-session-at-a-time is a fine assumption for v1 — no need for concurrency control beyond SQLite's defaults.
- All LLM/embedding backends should sit behind a thin interface (`LLMClient`, `EmbeddingClient`) so swapping Ollama for a hosted API later (if you ever want a "cloud mode") doesn't touch pipeline logic.
- Config via `.env` as already defined (`VIVA_DURATION_MINUTES`, `MAX_QUESTIONS`, `TOP_K_RETRIEVAL`, etc.) — plus `MAX_FILES=500` and `TEST_FILE_QUOTA_PCT=10`.
- Cloned repository code is never executed as part of analysis — static parsing (tree-sitter) only.
- Cloned repos and per-session indices follow a defined cleanup/retention policy rather than accumulating unbounded on disk across sessions.
