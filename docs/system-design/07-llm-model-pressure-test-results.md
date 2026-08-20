# LLM_MODEL Pressure-Test Results

Generated 2026-08-20 by `scripts/pressure_test_llm_model.py`. Closes `docs/system-design/04-open-questions.md` item 5.

N=10 repetitions per sample per model.

## Summary

| Model | Mean classification stability | Citation-compliance rate |
|---|---|---|
| `qwen2.5-coder:7b` | 94% | 8% |
| `qwen3.5:latest` | 76% | 67% |
| `deepseek-r1:latest` | 80% | 100% |
| `llama3:latest` | 100% | 30% |
| `nemotron-mini:latest` | 90% | 9% |
| `gemma4:e4b` | 88% | 61% |
| `mistral-nemo:12b` | 92% | 100% |

## `qwen2.5-coder:7b`

| Sample | Expected | Classifications | Stability | Citations |
|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| partial-1 | partial | correct, correct, correct, correct, partial, correct, incorrect, correct, correct, partial | 70% | 1/3 |
| incorrect-1 | incorrect | incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect | 100% | 0/10 |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | n/a |

## `qwen3.5:latest`

| Sample | Expected | Classifications | Stability | Citations |
|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, not_attempted, correct, correct, correct, correct, correct | 90% | n/a |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, not_attempted, not_attempted, not_attempted | 70% | n/a |
| partial-1 | partial | correct, correct, partial, correct, not_attempted, correct, partial, partial, correct, not_attempted | 50% | 2/3 |
| incorrect-1 | incorrect | not_attempted, not_attempted, not_attempted, incorrect, not_attempted, incorrect, not_attempted, not_attempted, incorrect, not_attempted | 70% | 2/3 |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | n/a |

## `deepseek-r1:latest`

| Sample | Expected | Classifications | Stability | Citations |
|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, correct, not_attempted, correct, correct, correct, not_attempted | 80% | n/a |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| partial-1 | partial | correct, not_attempted, not_attempted, correct, not_attempted, not_attempted, correct, not_attempted, not_attempted, not_attempted | 70% | n/a |
| incorrect-1 | incorrect | incorrect, not_attempted, incorrect, incorrect, partial, incorrect, not_attempted, not_attempted, not_attempted, incorrect | 50% | 6/6 |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | n/a |

## `llama3:latest`

| Sample | Expected | Classifications | Stability | Citations |
|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| partial-1 | partial | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| incorrect-1 | incorrect | incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect | 100% | 3/10 |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | n/a |

## `nemotron-mini:latest`

| Sample | Expected | Classifications | Stability | Citations |
|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| correct-2 | correct | not_attempted, correct, correct, not_attempted, not_attempted, not_attempted, correct, not_attempted, partial, not_attempted | 60% | 1/1 |
| partial-1 | partial | correct, correct, correct, correct, not_attempted, correct, correct, correct, correct, correct | 90% | n/a |
| incorrect-1 | incorrect | incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect | 100% | 0/10 |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | n/a |

## `gemma4:e4b`

| Sample | Expected | Classifications | Stability | Citations |
|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| partial-1 | partial | incorrect, partial, partial, correct, incorrect, incorrect, partial, incorrect, partial, correct | 40% | 7/8 |
| incorrect-1 | incorrect | incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect | 100% | 4/10 |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | n/a |

## `mistral-nemo:12b`

| Sample | Expected | Classifications | Stability | Citations |
|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| partial-1 | partial | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| incorrect-1 | incorrect | incorrect, correct, correct, correct, incorrect, correct, incorrect, incorrect, correct, correct | 60% | 4/4 |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | n/a |

## Recommendation

_Fill in after reviewing the tables above: which model becomes the new `LLM_MODEL` default in `.env.example`, and why. A model that is more stable but has worse citation compliance (or vice versa) is a real trade-off worth writing down here, not just picking the higher number on one axis._
