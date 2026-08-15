# System Design Reference — Part 2: Iteration Log

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. This log explains *why* the architecture
> in `03-final-architecture.md` (and the canonical `../design.md`) has the
> shape it does — kept as a record so future changes don't accidentally
> re-introduce a problem that was already found and fixed once.

## Iteration 1 (naive pass)

Straight linear pipeline: Ingest → Analyze → Index → Generate all questions upfront → Ask sequentially → Evaluate at the end → Report.

**Problems found tracing this through:**
- Pre-generating all questions upfront means the viva can't adapt to a weak answer (no follow-ups), which was an explicit goal.
- Evaluating everything "at the end" means the local model has to hold the entire transcript in context for evaluation — for an 8-question viva, this risks context overflow on a local model and loses grounding precision per answer.
- Nothing handles the case where the timer runs out mid-question, or where LLM generation latency itself eats the user's answering time.
- No resilience story — if the process crashes at minute 25, the whole viva is lost.
- The 500-file sampling decision has no owner in the pipeline — analysis and indexing were treated as one step, but they need different data (analysis needs the *whole* filtered set for the profile; indexing needs it chunked).

## Iteration 2 (fixes structural issues)

Split into a proper state machine with a session store, evaluate incrementally per Q&A instead of at the end, and separate analysis from indexing.

**Problems found tracing this through again:**
- Incremental evaluation right after each answer risks the evaluation LLM call blocking the *next* question's generation, wasting session time on eval work the user is just waiting through. Need to decide: is evaluation synchronous (safer, simpler, costs time) or deferred to background (faster viva, more complex)? → **Decision: defer evaluation to run in the background during the *next* question's "read + think" time, and if it hasn't finished by session end, run remaining evals during the summary-generation phase, not during the timed viva.** This keeps the user-facing clock honest.
- Adaptive follow-ups mean the question plan can't be fully rigid — need a lightweight planner that tracks category coverage and lets the runtime decide "follow up" vs "next category" turn by turn.
- Realized the Project Profile is being treated as just another RAG document — but it needs to be **always in context**, not retrieved, or architecture-level questions get poorly grounded. Needs its own storage path, separate from the vector store.

## Iteration 3 (final structural pass)

Added explicit component boundaries, clarified what's synchronous vs async, and defined the data contracts between components so each is independently testable — this became `03-final-architecture.md` / `../design.md`.

## Iteration 4 (post-validator review)

A Senior System Developer review of the Iteration-3 design and the accompanying `requirements.md`/`plan.md` surfaced five further gaps, all since incorporated:

1. **No early de-risking of the two shakiest assumptions.** Local-model structured-output reliability and the independent-timer model weren't exercised until late-stage phases (Phase 6/7 in the original plan). Fix: added **Phase 0 — Walking Skeleton** to `plan.md` — a thin end-to-end slice built first specifically to validate these two things before deeper component work begins.
2. **Evaluation treated as one atomic background step**, which conflicts with the fact that follow-up generation needs to know the classification result immediately. Fix: split scheduling explicitly — classification call synchronous, free-text feedback call backgrounded (documented in `01-resolved-decisions.md` §1.2 and `../design.md` §7).
3. **Chicken-and-egg in import-graph-based sampling** — the graph used to decide which files to keep was implicitly being built from the already-capped file set. Fix: the graph is now explicitly built from a cheap static scan over the full hard-exclusion-filtered set, before ranking (see `01-resolved-decisions.md` §1.1).
4. **No testing strategy in the plan.** Fix: added a cross-cutting requirement in `plan.md` for a small "golden repo" fixture set, reused across Phases 2–8, so quality regressions in the profile/question/evaluation outputs show up in CI rather than only via manual review.
5. **NFR7 (repo/index cleanup and retention) had no phase owner** in the original plan and risked being silently dropped. Fix: explicitly assigned to Phase 9 in `plan.md`.
