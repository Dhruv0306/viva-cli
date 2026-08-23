# LLM_MODEL Pressure-Test Results

Generated 2026-08-20 by `scripts/pressure_test_llm_model.py`. Closes `docs/system-design/04-open-questions.md` item 5.

N=10 repetitions per sample per model.

Stability and citation-compliance are computed only over valid model responses. Runs where the harness itself failed to get a response (timeout, connection error) are excluded from both metrics and reported separately as **Call errors** -- they say nothing about model quality. Runs where the model's own repair loop was exhausted (bad JSON twice) are NOT excluded -- that's a real structured-output reliability finding -- but are counted in **Needs review** so they stay visible rather than hiding inside a normal-looking verdict.

**Accuracy** (new) checks classification against the sample's expected label. A model can be perfectly self-consistent while being consistently wrong -- e.g. always classifying an incorrect answer as `correct` -- and stability alone will not catch that. Don't pick a model from the stability/citation columns alone; check accuracy first.

## Summary

| Model | Mean accuracy vs expected | Mean classification stability | Citation-compliance rate | Needs review | Call errors |
|---|---|---|---|---|---|
| `qwen2.5-coder:7b` | 80% | 100% | 0% | 10 | 0 |
| `qwen3.5:latest` | 90% | 72% | 43% | 4 | 10 |
| `deepseek-r1:latest` | 72% | 92% | 100% | 0 | 0 |
| `llama3:latest` | 80% | 100% | 40% | 6 | 0 |
| `nemotron-mini:latest` | 80% | 100% | 0% | 10 | 0 |
| `gemma4:e4b` | 90% | 90% | 53% | 8 | 0 |
| `mistral-nemo:12b` | 60% | 100% | n/a (no partial/incorrect verdicts) | 0 | 0 |

## `qwen2.5-coder:7b`

| Sample | Expected | Classifications (valid runs only) | Accuracy | Stability | Citations | Needs review | Call errors |
|---|---|---|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | 100% | n/a | 0 | 0 |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | 100% | n/a | 0 | 0 |
| partial-1 | partial | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 0% | 100% | n/a | 0 | 0 |
| incorrect-1 | incorrect | incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect | 100% | 100% | 0/10 | 10 | 0 |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | 100% | n/a | 0 | 0 |

## `qwen3.5:latest`

| Sample | Expected | Classifications (valid runs only) | Accuracy | Stability | Citations | Needs review | Call errors |
|---|---|---|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | 100% | n/a | 0 | 0 |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | 100% | n/a | 0 | 0 |
| partial-1 | partial | correct, partial, partial, partial, correct, partial, partial, partial, incorrect, correct | 60% | 60% | 3/7 | 4 | 0 |
| incorrect-1 | incorrect | n/a | n/a | 0% | n/a | 0 | 10 |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | 100% | n/a | 0 | 0 |

## `deepseek-r1:latest`

| Sample | Expected | Classifications (valid runs only) | Accuracy | Stability | Citations | Needs review | Call errors |
|---|---|---|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | 100% | n/a | 0 | 0 |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | 100% | n/a | 0 | 0 |
| partial-1 | partial | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 0% | 100% | n/a | 0 | 0 |
| incorrect-1 | incorrect | incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, partial, incorrect, incorrect, partial | 80% | 80% | 10/10 | 0 | 0 |
| blank-1 | not_attempted | not_attempted, correct, not_attempted, not_attempted, correct, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 80% | 80% | n/a | 0 | 0 |

## `llama3:latest`

| Sample | Expected | Classifications (valid runs only) | Accuracy | Stability | Citations | Needs review | Call errors |
|---|---|---|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | 100% | n/a | 0 | 0 |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | 100% | n/a | 0 | 0 |
| partial-1 | partial | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 0% | 100% | n/a | 0 | 0 |
| incorrect-1 | incorrect | incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect | 100% | 100% | 4/10 | 6 | 0 |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | 100% | n/a | 0 | 0 |

## `nemotron-mini:latest`

| Sample | Expected | Classifications (valid runs only) | Accuracy | Stability | Citations | Needs review | Call errors |
|---|---|---|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | 100% | n/a | 0 | 0 |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | 100% | n/a | 0 | 0 |
| partial-1 | partial | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 0% | 100% | n/a | 0 | 0 |
| incorrect-1 | incorrect | incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect | 100% | 100% | 0/10 | 10 | 0 |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | 100% | n/a | 0 | 0 |

## `gemma4:e4b`

| Sample | Expected | Classifications (valid runs only) | Accuracy | Stability | Citations | Needs review | Call errors |
|---|---|---|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | 100% | n/a | 0 | 0 |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | 100% | n/a | 0 | 0 |
| partial-1 | partial | partial, partial, correct, partial, partial, partial, incorrect, correct, incorrect, correct | 50% | 50% | 4/7 | 3 | 0 |
| incorrect-1 | incorrect | incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect | 100% | 100% | 5/10 | 5 | 0 |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | 100% | n/a | 0 | 0 |

## `mistral-nemo:12b`

| Sample | Expected | Classifications (valid runs only) | Accuracy | Stability | Citations | Needs review | Call errors |
|---|---|---|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | 100% | n/a | 0 | 0 |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | 100% | n/a | 0 | 0 |
| partial-1 | partial | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 0% | 100% | n/a | 0 | 0 |
| incorrect-1 | incorrect | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 0% | 100% | n/a | 0 | 0 |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | 100% | n/a | 0 | 0 |

## Recommendation

**`gemma4:e4b`**, replacing `qwen2.5-coder:7b` as `LLM_MODEL`'s default.

Note: this run's headline "Mean accuracy vs expected" numbers above were
computed before a metric bug was fixed (see `git log` on this file/
`scripts/pressure_test_llm_model.py`) -- `qwen3.5:latest`'s 90% was
inflated by silently excluding its one totally-failed sample instead of
counting it as 0%. Corrected, `qwen3.5:latest` is 72%, not 90%. The ranking
below uses the corrected numbers.

Ranked best to worst, weighing accuracy first, then how *dangerous* each
model's specific failure mode is (confidently wrong is worse than
inconsistent, which is worse than ungrounded, which is worse than missing
one nuance):

1. **`gemma4:e4b` -- 90% accuracy, 90% stability, 53% citations, 0 call
   errors.** The only model of the 7 tested that shows real signal on
   `partial-1` (50% accuracy there) instead of defaulting to `correct`
   every time. Best overall pick.
2. `deepseek-r1:latest` -- 72% accuracy, 92% stability, **100% citations**,
   0 needs_review. Best grounding by far, but never catches `partial-1`
   (0%) and occasionally grades a blank answer `correct` (2/10 on
   `blank-1`) -- a real, narrow flaw. Pick this instead if grounding
   matters more to you than partial-correctness detection.
3. `llama3:latest` -- 80% accuracy, 100% stability, 40% citations.
   Perfectly consistent, no glaring qualitative flaw, but never catches
   `partial-1` and citation grounding is middling.
4. `qwen2.5-coder:7b` (current default) -- 80% accuracy, 100% stability,
   **0% citations**. Every single incorrect verdict is ungrounded (0/10
   cited on `incorrect-1`) -- 100% of its wrong-answer calls would need
   manual review in production.
5. `nemotron-mini:latest` -- statistically identical to `qwen2.5-coder:7b`,
   same 0% citation problem.
6. `qwen3.5:latest` -- 72% corrected accuracy, 72% stability, 43%
   citations, and **`incorrect-1` failed all 10 calls** (excluded from
   stability/citation stats as call errors, not model behavior, but still
   disqualifying for reliability -- a live viva session hitting this
   combination could hang or drop a question). Also flip-flops on
   `partial-1` (60% stability) even on the calls that do succeed.
7. **`mistral-nemo:12b` -- ruled out.** 60% accuracy, 100% stability: it
   reproducibly marks a genuinely wrong answer `correct` 10/10 times on
   `incorrect-1`. Confidently wrong is the single worst failure mode for a
   grader and this is not noise -- do not reconsider this model without a
   fresh, larger pressure-test run.

**Known limitation, not a model-selection issue:** every model except
`gemma4:e4b` (and unstably, `qwen3.5:latest`) gets `partial-1` wrong 100%
of the time, always defaulting to `correct`. Given how consistent this is
across otherwise-different models, it's worth revisiting whether the
`partial-1` fixture sample itself is ambiguous or too subtle, independent
of which model gets picked.

Considered and explicitly declined for now: routing `LLM_MODEL` through a
free cloud API (NVIDIA NIM, OpenRouter) to access larger frontier-class
models. Sending student code to a third party conflicts with `NFR1` (local
stack, no required paid/external calls) and the project's local-only
privacy story, and free-tier rate limits (~20-40 req/min, and OpenRouter's
50 req/day on an unfunded account) are a bad fit for a live timed session.
Not revisited unless `NFR1` itself is deliberately revised.

