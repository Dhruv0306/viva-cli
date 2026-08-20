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

_Fill in after reviewing the tables above: which model becomes the new `LLM_MODEL` default in `.env.example`, and why. A model that is more stable but has worse citation compliance (or vice versa) is a real trade-off worth writing down here, not just picking the higher number on one axis. Also weigh Call errors (infra reliability on your hardware, not model quality) and Needs review (how often the model's own structured-output reliability broke down) -- a model that wins on stability/citations but needs review constantly is still a bad pick for an unattended pipeline._
