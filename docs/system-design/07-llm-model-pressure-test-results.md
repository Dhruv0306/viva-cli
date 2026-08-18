# LLM_MODEL Pressure-Test Results

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts.

**Not yet run.** This file is a placeholder; it gets overwritten by running:

```bash
python scripts/pressure_test_llm_model.py \
  --output docs/system-design/07-llm-model-pressure-test-results.md
```

against a local Ollama with both candidate models pulled (`ollama pull
qwen2.5-coder:7b`, `ollama pull qwen3.5:latest`). See
`04-open-questions.md` item 5 for the question this closes, and the
docstring in `scripts/pressure_test_llm_model.py` for methodology.

Once real results are written here, update `.env.example`'s `LLM_MODEL`
default (and the README config table) if the evidence points to a
different model than the current placeholder default, and mark
`04-open-questions.md` item 5 fully resolved with a link to this file.
