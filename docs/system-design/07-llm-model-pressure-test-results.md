# LLM_MODEL Pressure-Test Results

Generated 2026-08-19 by `scripts/pressure_test_llm_model.py`. Closes `docs/system-design/04-open-questions.md` item 5.

N=10 repetitions per sample per model.

## Summary

| Model | Mean classification stability | Citation-compliance rate |
|---|---|---|
| `qwen2.5-coder:7b` | 100% | 0% |
| `qwen3.5:latest` | 90% | 80% |
| `deepseek-r1:latest` | 96% | 100% |
| `llama3:latest` | 100% | 70% |

## `qwen2.5-coder:7b`

| Sample | Expected | Classifications | Stability | Citations |
|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| partial-1 | partial | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| incorrect-1 | incorrect | incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect | 100% | 0/10 |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | n/a |

## `qwen3.5:latest`

| Sample | Expected | Classifications | Stability | Citations |
|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| partial-1 | partial | partial, correct, partial, correct, correct, partial, correct, correct, partial, partial | 50% | 4/5 |
| incorrect-1 | incorrect | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | n/a |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | n/a |

## `deepseek-r1:latest`

| Sample | Expected | Classifications | Stability | Citations |
|---|---|---|---|---|
| correct-1 | correct | correct, correct, not_attempted, correct, correct, not_attempted, correct, correct, correct, correct | 80% | n/a |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| partial-1 | partial | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| incorrect-1 | incorrect | incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect | 100% | 10/10 |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | n/a |

## `llama3:latest`

| Sample | Expected | Classifications | Stability | Citations |
|---|---|---|---|---|
| correct-1 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| correct-2 | correct | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| partial-1 | partial | correct, correct, correct, correct, correct, correct, correct, correct, correct, correct | 100% | n/a |
| incorrect-1 | incorrect | incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect, incorrect | 100% | 7/10 |
| blank-1 | not_attempted | not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted, not_attempted | 100% | n/a |

## Recommendation

_Fill in after reviewing the tables above: which model becomes the new `LLM_MODEL` default in `.env.example`, and why. A model that is more stable but has worse citation compliance (or vice versa) is a real trade-off worth writing down here, not just picking the higher number on one axis._
