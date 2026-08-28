"""Tests for viva.analyzer.reduce -- module-level reduce and the
recursive hierarchical reduce (docs/system-design/06-cli-contract-and-profile-scaling.md
§6.2), including the forced-batching path via an artificially low
`max_reduce_context_tokens` per the agreed test strategy (fast/deterministic
unit coverage of the recursion logic, separate from the manual
profile-quality review fixture).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from viva.analyzer.models import FileSummary
from viva.analyzer.reduce import (
    _resolve_max_reduce_context_tokens,
    build_architecture_summary,
    hierarchical_reduce,
    reduce_module,
)
from viva.config import Config


def _config(map_reduce_batch_size: int = 8, max_reduce_context_tokens: int | None = None) -> Config:
    return Config(
        llm_model="test-model", embedding_model="nomic-embed-text", temperature=0.3,
        ollama_host="http://localhost:11434", viva_duration_minutes=30, max_questions=8,
        max_followup_depth=1, session_retention_days=7, max_files=500, test_file_quota_pct=10,
        github_token=None, map_reduce_batch_size=map_reduce_batch_size,
        max_reduce_context_tokens=max_reduce_context_tokens, line_window_size=60,
        line_window_overlap=15, vector_db_path="./data/chroma", top_k_retrieval=5,
        session_db_path="./data/viva.db", avg_time_per_category_seconds=180,
    )


def _mock_llm(context_window: int | None = None):
    llm = MagicMock()
    llm.reduce.side_effect = lambda label, summaries, target_tokens: f"reduced({label})"
    llm.get_context_window.return_value = context_window
    return llm


def test_single_text_returned_without_llm_call():
    llm = _mock_llm()
    result = hierarchical_reduce("label", ["only one"], llm, _config())

    assert result == "only one"
    llm.reduce.assert_not_called()


def test_empty_texts_returns_empty_string():
    llm = _mock_llm()
    assert hierarchical_reduce("label", [], llm, _config()) == ""


def test_small_input_reduces_directly_in_one_call():
    llm = _mock_llm()
    texts = ["a", "b", "c"]

    result = hierarchical_reduce("label", texts, llm, _config(max_reduce_context_tokens=1000))

    assert llm.reduce.call_count == 1
    assert result == "reduced(label)"


def test_batch_count_overflow_triggers_recursive_batching():
    # 10 texts with batch_size=3 forces batching regardless of token size.
    llm = _mock_llm()
    texts = [f"summary {i}" for i in range(10)]

    result = hierarchical_reduce("label", texts, llm, _config(map_reduce_batch_size=3, max_reduce_context_tokens=100_000))

    # 10 texts / batch_size 3 -> 4 batches -> 4 reduce calls at level 1,
    # then those 4 batch summaries (<= batch_size 3? no, 4 > 3, so another
    # level) -- either way, more than one reduce call and a final result.
    assert llm.reduce.call_count >= 2
    assert result.startswith("reduced(")


def test_token_overflow_alone_routes_through_the_batching_branch():
    # Long texts that individually fit but together exceed a tiny token
    # budget. With item count <= batch_size there's only ever one
    # possible grouping, so this can't change the *call count* vs. the
    # direct-reduce case -- but it must still route through the
    # batch-labeling branch (proven by the "part 1 of 1" label) rather
    # than silently using the un-batched label, since that's the
    # user-visible signal that the size check actually fired.
    llm = _mock_llm()
    texts = ["x" * 400 for _ in range(3)]  # ~100 tokens each at chars/4

    result = hierarchical_reduce("label", texts, llm, _config(map_reduce_batch_size=8, max_reduce_context_tokens=150))

    assert llm.reduce.call_count == 1
    assert llm.reduce.call_args.args[0] == "label (part 1 of 1)"
    assert result.startswith("reduced(")


def test_token_overflow_with_enough_items_forces_multiple_calls():
    # Once item count exceeds batch_size, a tiny token budget forces
    # real multi-batch recursion, not just a relabeled single call.
    llm = _mock_llm()
    texts = [f"summary {i}" for i in range(10)]

    result = hierarchical_reduce("label", texts, llm, _config(map_reduce_batch_size=8, max_reduce_context_tokens=1))

    assert llm.reduce.call_count >= 2
    assert result.startswith("reduced(")


def test_reduce_module_returns_module_summary_with_file_count():
    llm = _mock_llm()
    file_summaries = [
        FileSummary(path="a.py", module="src", parse_method="ast", summary="does a"),
        FileSummary(path="b.py", module="src", parse_method="ast", summary="does b"),
    ]

    result = reduce_module("src", file_summaries, llm, _config(max_reduce_context_tokens=1000))

    assert result.module == "src"
    assert result.file_count == 2
    assert result.summary == "reduced(Module: src)"


def test_build_architecture_summary_formats_module_pairs():
    from viva.analyzer.models import ModuleSummary

    llm = _mock_llm()
    modules = [
        ModuleSummary(module="src", summary="does src stuff", file_count=3),
        ModuleSummary(module="tests", summary="tests things", file_count=2),
    ]

    build_architecture_summary(modules, llm, _config(max_reduce_context_tokens=1000))

    passed_texts = llm.reduce.call_args.kwargs.get("summaries") or llm.reduce.call_args.args[1]
    assert any("src: does src stuff" in t for t in passed_texts)


def test_resolve_uses_configured_value_when_set():
    llm = _mock_llm(context_window=8192)
    assert _resolve_max_reduce_context_tokens(llm, _config(max_reduce_context_tokens=500)) == 500


def test_resolve_falls_back_to_fraction_of_model_context_window():
    llm = _mock_llm(context_window=8192)
    assert _resolve_max_reduce_context_tokens(llm, _config()) == 4096


def test_resolve_falls_back_to_hardcoded_default_when_context_window_unknown():
    llm = _mock_llm(context_window=None)
    result = _resolve_max_reduce_context_tokens(llm, _config())
    assert result == 3000
