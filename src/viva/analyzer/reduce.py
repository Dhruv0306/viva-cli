"""FR7/FR8 Reduce step: per-module reduce, and the recursive
hierarchical reduce for the single top-level `architecture_summary`
field (docs/system-design/06-cli-contract-and-profile-scaling.md §6.2).

Per §6.2's explicit consequence for the schema: `modules[]` always lists
every module's own summary at full Level-1 granularity regardless of how
many higher levels the architecture-summary recursion needed -- so
`reduce_module()` always returns exactly one `ModuleSummary` per module,
and only `build_architecture_summary()`'s internal recursion depth
varies with repo size.
"""
from __future__ import annotations

from viva.analyzer.models import FileSummary, ModuleSummary
from viva.analyzer.tokens import estimate_tokens
from viva.config import Config
from viva.llm_client import LLMClient

_TARGET_REDUCE_TOKENS = 200
# Used only when the user hasn't set MAX_REDUCE_CONTEXT_TOKENS and the
# model's real context window can't be determined at runtime (see
# LLMClient.get_context_window) -- a conservative floor that stays well
# under even a modest 4096-token local model's context, leaving room for
# the system prompt, the batch of summaries themselves, and the output.
_FALLBACK_MAX_REDUCE_CONTEXT_TOKENS = 3000
_CONTEXT_WINDOW_FRACTION = 0.5


def reduce_module(
    module: str, file_summaries: list[FileSummary], llm_client: LLMClient, config: Config
) -> ModuleSummary:
    texts = [fs.summary for fs in file_summaries]
    combined = hierarchical_reduce(f"Module: {module}", texts, llm_client, config)
    return ModuleSummary(module=module, summary=combined, file_count=len(file_summaries))


def build_architecture_summary(
    module_summaries: list[ModuleSummary], llm_client: LLMClient, config: Config
) -> str:
    texts = [f"{m.module}: {m.summary}" for m in module_summaries]
    return hierarchical_reduce("Overall project architecture", texts, llm_client, config)


def hierarchical_reduce(
    label: str, texts: list[str], llm_client: LLMClient, config: Config
) -> str:
    """Standard tree summarization (§6.2): reduce directly if `texts`
    fits the size check, otherwise batch-and-recurse.
    """
    if not texts:
        return ""
    if len(texts) == 1:
        return texts[0]

    max_tokens = _resolve_max_reduce_context_tokens(llm_client, config)
    fits_size = estimate_tokens("\n\n".join(texts)) <= max_tokens
    fits_batch = len(texts) <= config.map_reduce_batch_size

    if fits_size and fits_batch:
        return llm_client.reduce(label, texts, target_tokens=_TARGET_REDUCE_TOKENS)

    batch_size = config.map_reduce_batch_size
    batches = [texts[i : i + batch_size] for i in range(0, len(texts), batch_size)]
    batch_summaries = [
        llm_client.reduce(
            f"{label} (part {i + 1} of {len(batches)})", batch, target_tokens=_TARGET_REDUCE_TOKENS
        )
        for i, batch in enumerate(batches)
    ]
    # Batching strictly reduces the item count each recursion (batch_size
    # >= 2 is enforced by Config's positive-int validation, and a batch
    # of 1 leftover item still collapses via the len(texts) == 1 base
    # case above), so this always terminates.
    return hierarchical_reduce(label, batch_summaries, llm_client, config)


def _resolve_max_reduce_context_tokens(llm_client: LLMClient, config: Config) -> int:
    if config.max_reduce_context_tokens is not None:
        return config.max_reduce_context_tokens

    context_window = llm_client.get_context_window()
    if context_window:
        return int(context_window * _CONTEXT_WINDOW_FRACTION)

    return _FALLBACK_MAX_REDUCE_CONTEXT_TOKENS
