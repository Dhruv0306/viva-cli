# System Design Reference — Part 1: Resolved Design Decisions

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts, and `../design.md` for the canonical,
> build-facing design these decisions feed into.

## 1.1 Repo Size Limit & Sampling Strategy (500 files)

Filtering happens in two passes, not one:

**Pass A — Hard exclusion (before counting toward the 500 cap)**
- `.git`, `node_modules`, `venv`, `__pycache__`, `dist`, `build`, `target`, vendor dirs
- Binary files, images, lockfiles (`package-lock.json`, `poetry.lock`, etc.), minified files
- Files over a size threshold (e.g. 200KB) — usually generated/data files, not hand-written logic

**Pass B — Priority ranking (if remaining files > 500)**
Score each file and take the top 500:

| Signal | Why |
|---|---|
| Always-include tier | `README*`, entry points (`main.*`, `app.*`, `manage.py`, `index.*`), manifest files (`package.json`, `requirements.txt`, `pom.xml`) — these don't count against the 500, they're structural context |
| Import-graph centrality | Build a lightweight import/require graph; files imported by many others rank higher — these are architectural "hubs" |
| Path heuristics | `src/`, `core/`, `lib/`, `app/` ranked above `tests/`, `scripts/`, `examples/` (tests still get a guaranteed minimum quota — see below) |
| Directory-stratified sampling | Once ranked, allocate the budget **proportionally across top-level directories/modules**, not globally — otherwise one large module (e.g. `frontend/`) can crowd out a smaller but architecturally important one (e.g. `auth/`). This is the standard fix for representativeness in RAG corpus construction. |
| Guaranteed test quota | Reserve ~10% of budget for test files specifically, since "testing strategy" is a viva question category — if tests are cut, that category degrades silently |

**Transparency requirement:** the Project Profile must record *what was excluded* (`"analyzed 500/823 files, prioritized by import centrality and directory coverage"`). This gets passed to the Question Generator so it never asks about files it never saw, and gets shown to the user at session start so expectations are set correctly.

**Post-validator refinement (see Part 2, Iteration 3 note in the changelog):** the import graph itself must be built from a cheap static scan (regex/AST import-statement parsing only, no LLM call) run over the *full* hard-exclusion-filtered set — not over the already-capped 500. Building the graph after capping would be circular: you can't rank files by how many other files reference them if the referencing files were already excluded before the graph existed.

## 1.2 Structured Output Reliability (local model, zero-cost)

Three layers, cheapest/most reliable first:

1. **Grammar-constrained decoding at the inference layer.** Use Ollama's structured output support (JSON-schema-constrained generation via GBNF grammar under the hood) rather than "please respond in JSON" prompting. This makes malformed JSON structurally impossible, not just less likely.
2. **Pydantic schema validation on receipt.** Every LLM call that needs structured data has a corresponding Pydantic model. Parse immediately; don't pass raw text further down the pipeline.
3. **Decompose large outputs into smaller structured calls.** The evaluation output (`summary`, `did_well`, `missed`, `did_wrong`, `improvement`) is 5 fields — small local models degrade in JSON reliability as field count and free-text length grow. Instead of one call producing all 5 fields, use 2 calls: one classifying (`correct | partial | incorrect | not_attempted` — small, easy schema), one generating the free-text feedback conditioned on that classification. Smaller schemas per call = higher success rate.
4. **Repair loop, not silent failure.** On validation failure: re-prompt once with the validation error appended ("Your last response failed because X — return valid JSON only"). On second failure: fall back to regex/heuristic extraction. On third failure: mark that record `needs_review: true` and continue — never block the whole viva on one bad parse.

**Concurrency consequence of the 2-call split (post-validator refinement):** because follow-up question generation depends on knowing whether the previous answer was correct/partial/wrong, the *classification* call must run synchronously right after the answer is submitted. Only the second, heavier free-text feedback call is safe to defer to the background. Treating "evaluation" as one atomic background unit (as an earlier design iteration did) creates a race between follow-up generation and evaluation completion — see Part 2, Iteration 2 → 3.

## 1.3 "Ground Truth" Framing (system prompt design)

Ground truth = **the retrieved code, not the LLM's general knowledge of best practices.** This has to be enforced structurally, not just requested politely:

- The evaluator prompt is built in explicitly labeled sections: `[QUESTION]`, `[GROUND_TRUTH_CODE_CONTEXT]`, `[USER_ANSWER]` — never concatenated as flowing text. Section boundaries reduce the model conflating "what the code does" with "what it thinks is best practice."
- System prompt for the evaluator role explicitly states: *judge only against the provided code context; if the code doesn't clearly show something, do not penalize the user for not mentioning it; do not import outside best-practice opinions unless the code contradicts them.*
- Every `missed` / `did_wrong` item must cite a specific file/function from the retrieved context — if the model can't produce a citation, that item is dropped rather than kept. This is a strong forcing function against hallucinated criticism.
- Each pipeline stage (analysis / question-gen / evaluation) gets its **own** system prompt with stage-specific grounding language — a single shared system prompt tends to blur these constraints over a long session.
