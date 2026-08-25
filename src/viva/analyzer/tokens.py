"""Shared token-count heuristic for the Analyzer's map-reduce sizing
checks (docs/system-design/06-cli-contract-and-profile-scaling.md §6.2).

Deliberately a cheap chars/4 estimate, not a real per-model tokenizer:
`Config.max_reduce_context_tokens` is documented as "a conservative
default... to leave room" under the model's real context window, so an
approximate-but-conservative estimate is consistent with that intent, and
avoids depending on tokenizer compatibility with whatever `LLM_MODEL` is
configured (which may be any model Ollama can serve).
"""
from __future__ import annotations

_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(len(text) // _CHARS_PER_TOKEN, 1)
