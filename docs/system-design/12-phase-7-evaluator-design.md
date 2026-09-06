# System Design Reference — Part 12: Phase 7 Evaluator Design

## 12.1 Scope

FR21–FR24: real per-answer evaluation. Phase 6 built every seam this phase
fills — `ClassificationProvider` (inert `NullClassificationProvider`),
`qa_records.eval_status`/`eval_json` (always `'deferred'`/`NULL`), and the
`FINALIZING_EVALS` state (currently a no-op passthrough). Phase 7 adds no
new orchestrator states and no new CLI commands; it replaces null
implementations with real ones.

Explicitly out of scope (per `docs/plan.md`): `viva report` rendering of
`eval_json` (Phase 8) and any change to the FR14 follow-up *decision*
logic itself, which Phase 6 already built against `Classification` values.

## 12.2 Restructuring `evaluate_answer` into a two-call Evaluator

Phase 0's `LLMClient.evaluate_answer()` is retired as a single entrypoint.
Per design.md §4's small-schema-decomposition strategy, it splits into two
calls on `LLMClient`, both following the existing 2-attempt
schema-validation repair loop and graceful-fallback pattern:

- **`classify_answer(question, ground_truth_context, user_answer) -> LLMCallResult[ClassificationResult]`**
  — the fast call. `ClassificationResult` is `EvaluationResult` renamed
  (`classification`, `summary`, `cited_file`, `needs_review`), same FR22
  application-layer downgrade (ungrounded `partial`/`incorrect` →
  `needs_review=True`). This is what `ClassificationProvider.classify()`
  needs, synchronously, to drive FR14.

- **`generate_feedback(question, ground_truth_context, user_answer, classification) -> LLMCallResult[EvaluationFeedback]`**
  — the slow call, run second and given the verdict as context (a
  `correct` verdict doesn't need `did_wrong` prompted for; an `incorrect`
  one doesn't need `did_well` padded out). New schema:

  ```python
  class MissedPoint(BaseModel):
      point: str
      cited_file: str | None = None

  class EvaluationFeedback(BaseModel):
      did_well: list[str]
      missed: list[MissedPoint]
      did_wrong: list[MissedPoint]
      improvement: str
      needs_review: bool = False
  ```

  Same FR22 rule, applied per-item: any `missed`/`did_wrong` entry whose
  `cited_file` isn't among the grounding chunks' file paths is dropped. If
  dropping empties both lists while the classification was
  `partial`/`incorrect`, `needs_review` is forced `True` — an
  unsubstantiated critical verdict is worse than an admittedly-incomplete
  one.

  `needs_review` on both `ClassificationResult` and `EvaluationFeedback`
  is client-set, never model-set — real-world validation against
  `gemma4:e4b` showed the model populating it unprompted otherwise, with
  no way to tell afterward whether a `true` came from FR22 logic or the
  model's own opinion. `LLMClient` enforces this at two points: the field
  is stripped from the schema handed to the model's constrained decoding
  (`_model_facing_schema()`), and reset to `False` immediately after
  parsing, before any FR22 logic runs, discarding it even if a model
  ignores the schema and sends it anyway.

A new `Evaluator` (`src/viva/evaluator.py`) owns both calls plus
persistence and is the real `ClassificationProvider` the orchestrator
constructs in place of `NullClassificationProvider`. `EvaluationRecord`
(classification + summary + did_well + missed + did_wrong + improvement +
needs_review — design.md §6) is the shape written to `qa_records.eval_json`.

## 12.3 Ground-truth reconstruction: `VectorStore.get_by_ids`

`qa_records.grounding_chunk_ids` is already persisted per FR13, but
`indexer/store.py`'s `VectorStore` only exposes `.query()` (semantic
search) — there's no by-ID fetch. Both Evaluator calls need the exact
chunks a question was generated from, not a fresh semantic search (which
could drift). New method:

```python
def get_by_ids(self, ids: list[str]) -> list[Chunk]
```

wrapping Chroma's native `collection.get(ids=...)`. `Evaluator` joins the
returned chunks with the same `"\n\n---\n\n"` convention
`questiongen/__init__.py` already uses, so both calls see
`grounding_context` in the same shape QuestionGen used to write the
question — consistency the grading prompt depends on.

## 12.4 Backgrounding: single worker thread + queue, not a thread per answer

`RichSessionUI`'s one long-lived background stdin-read thread is the
precedent, not N ephemeral threads. `Evaluator` starts one daemon worker
thread when the orchestrator enters `IN_PROGRESS`, consuming a
`queue.Queue[str]` of `qa_id`s:

- `Evaluator.classify()` (the `ClassificationProvider` method, called
  synchronously right after answer submission) runs `classify_answer()`
  inline, persists `eval_status='classified'` with a partial `eval_json`
  (classification only) immediately — durable before any backgrounding
  happens — then enqueues the `qa_id` and returns the `Classification` to
  the orchestrator so FR14 isn't blocked.
- The worker thread pulls one `qa_id` at a time, re-reads the persisted
  row, runs `generate_feedback()`, merges the result into `eval_json`, and
  sets `eval_status` to `complete` or `needs_review`.

One worker thread avoids concurrent SQLite writers (the existing
`SessionStore` isn't designed for multi-writer access) and gives strict
ordering, at the cost of feedback for answer *N* possibly still running
while the user reads question *N+1* — the acceptable overlap design.md §7
asks for. If the user answers faster than the worker drains, the queue
simply grows; nothing blocks the live loop.

### Known limitation: single-model Ollama serializes inference, so "background" doesn't mean "concurrent" at the model

Real-world validation (a live session against `gemma4:e4b`, one Ollama
instance, no `OLLAMA_NUM_PARALLEL` override) surfaced a gap between this
section's reasoning and what actually happens: the background thread is
real Python-level concurrency, but both the main thread's next
`classify_answer`/`generate_question` call and the worker thread's
`generate_feedback` call are HTTP requests to the *same* Ollama server.
With one model loaded and no parallel-request override, Ollama processes
one inference request at a time — so whichever call reaches the server
first occupies the model, and the other queues behind it regardless of
which Python thread issued it. The validation run saw this manifest as
per-question delays ranging from a few seconds up to roughly a minute,
comfortably inside `EVAL_FLUSH_TIMEOUT_SECONDS`'s default (60s) but real
and noticeable.

This isn't a bug in the queue/thread/lock mechanics — those behave
exactly as designed. It's a resource-contention ceiling this design
doesn't remove, only backgrounds around: real overlap between the next
question and the previous answer's feedback call requires the model
server itself to be able to run two requests concurrently, which a
single-model Ollama instance without `OLLAMA_NUM_PARALLEL >= 2` cannot
do. Whether to raise that setting (hardware/GPU-memory permitting) is a
deployment decision outside this design's scope, not something the
Evaluator should assume a default for.

## 12.5 `eval_status` state model

Four states, matching the granular model: `deferred` (Phase 6 default,
before any call runs) → `classified` (call 1 done, persisted) →
`feedback_pending` (enqueued, call 2 not yet started/finished) →
terminal `complete` or `needs_review`. `needs_review` is reachable from
either call (FR22 downgrades) or from a flush timeout (12.6).

## 12.6 `FINALIZING_EVALS` and resume

At `FINALIZING_EVALS`, the orchestrator calls `Evaluator.flush()`: pushes
a sentinel onto the queue and joins the worker thread with a bounded
timeout (`EVAL_FLUSH_TIMEOUT_SECONDS`, new config, default 60s) so session
end can't hang indefinitely on a stuck model call. Any `qa_record` still
`feedback_pending` when the timeout hits is marked `needs_review` with
`eval_json` left at classification-only — degraded but never lost,
per NFR3.

On `viva resume`, any `qa_record` loaded with `eval_status` in
`('classified', 'feedback_pending')` — meaning a prior process died
mid-evaluation — is re-enqueued to the new worker at startup.
`grounding_chunk_ids` plus the persisted question/answer text is
sufficient to fully regenerate feedback; nothing about this is
time-sensitive or session-local, so silent re-derivation on resume is
safe and requires no user-facing message.

## 12.7 New config (FR28: env-driven, no hardcoding)

- `EVAL_FLUSH_TIMEOUT_SECONDS` (default `60`) — bound on the
  `FINALIZING_EVALS` worker-thread join.

No new model/temperature config: both Evaluator calls reuse the existing
`LLM_MODEL`/`LLM_TEMPERATURE` settings, consistent with QuestionGen and
Phase 0.

## 12.8 Migration / blast radius

`EvaluationResult` → `ClassificationResult` is a rename with call-site
updates, not a behavior change, in:

- `src/viva/schemas.py` — rename; add `MissedPoint`, `EvaluationFeedback`,
  `EvaluationRecord`.
- `src/viva/llm_client.py` — split `evaluate_answer` into
  `classify_answer` + `generate_feedback`.
- `src/viva/phase0_demo.py` — updates its `EvaluationResult` import/usage;
  the Phase 0 walking-skeleton demo keeps working against
  `classify_answer` alone (it never needed free-text feedback).
- Tests referencing `evaluate_answer`/`EvaluationResult`: `test_schemas.py`,
  `test_llm_client.py`, `test_phase0_demo.py`,
  `test_pressure_test_llm_model.py`, `test_analyzer_integration.py`,
  `test_questiongen_integration.py`, `test_indexer_integration.py`,
  `tests/fixtures/pressure_test_samples.json` — mechanical rename plus new
  fixtures for `generate_feedback`.
- `docs/system-design/04-open-questions.md`,
  `08-phase-3-analyzer-design.md`, `10-phase-5-questiongen-design.md` —
  references to `evaluate_answer` updated to point at this doc.

## 12.9 Explicitly out of scope

- `viva report` and any human-facing rendering of `eval_json` (Phase 8).
- Changing FR14's follow-up trigger logic itself — Phase 6's consumption
  of `Classification` is unchanged; only what produces it is real now.
- Multi-worker/parallel evaluation — one background thread is sufficient
  at expected local-model throughput and avoids the SQLite multi-writer
  problem outright.

## 12.10 Real-world bug found later (Phase 10 testing): `classify()`'s latency wasn't excluded

Found well after this phase merged, during Phase 10 (web UI) testing —
see `docs/system-design/15-phase-10-web-ui-design.md` §15.13 for the
user-visible symptom (the web UI's countdown resumed lower than where
it had been frozen, once per question) that led here. The actual bug is
in `orchestrator.py`'s live loop, not the web UI: `self.ui.read_answer()`
returns, `record_answer()` persists the answer, and then
`_maybe_queue_followup()` is called directly — which calls
`self.classification_provider.classify()`. §12.2 above already
establishes that `classify()` makes a real, synchronous LLM call (the
fast classification half of the two-call split); FR17/FR24 both require
that latency not count against the person's timed session, the same as
`generate_question()`'s latency already doesn't (wrapped in
`timer.excluding()` at its own call sites in the same loop). The call to
`_maybe_queue_followup()` was never wrapped the same way — an oversight
in this phase, not a deliberate choice: nothing in this doc's design
discussion (§12.2's restructuring, §12.4's backgrounding) argued
`classify()`'s latency should count differently from
`generate_question()`'s.

Silent on the CLI: `RichSessionUI` only renders a live countdown while
actually blocked in `read_answer()`, not during the gap between
questions, so the extra unexcluded time was never visible there --
though it was still being spent for real, meaning every session using a
real `Evaluator` got slightly less actual think-time than its configured
`viva_duration_minutes`, silently, on every answer. The web UI's more
literal countdown display (freeze-then-resume,
`15-phase-10-web-ui-design.md` §15.13) is what finally made it visible
enough to report.

Fixed: the call to `_maybe_queue_followup()` in `orchestrator.py`'s live
loop is now wrapped in `timer.excluding()`, matching
`generate_question()`'s existing pattern exactly.

Regression test: `test_classification_latency_is_excluded_from_the_answer_timer`
in `test_orchestrator.py` — injects a `ClassificationProvider` whose
`classify()` sleeps a known duration, and asserts the drop in
`timer.remaining()` between two consecutive `read_answer()` calls is
much smaller than that sleep. Confirmed to fail against the pre-fix
code first (the drop matched the sleep almost exactly), then pass
against the fix.
