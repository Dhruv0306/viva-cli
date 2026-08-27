"""FR13 grounding retrieval, plus the fix for
docs/system-design/04-open-questions.md item 6 (retrieval-quality: a bare
category/module query lexically collided with unrelated test-fixture
chunks rather than semantically matching real implementation code).

Two complementary mitigations, per
docs/system-design/10-phase-5-questiongen-design.md §10.4:

1. **Query reformulation** (primary fix): never embed a bare category/
   module name. `build_query()` expands it into a richer natural-language
   query using the target module's own summary text, which carries real
   domain vocabulary instead of a short string prone to lexical collision.
2. **Test-path post-filter** (belt-and-suspenders): even a well-formed
   query can still surface test chunks ahead of implementation chunks for
   thin modules. `_is_test_path()` deprioritizes/excludes them for every
   category except `testing_strategy`, where test chunks are exactly what
   should be preferred. This is a heuristic on `Chunk.filepath` mirroring
   `ingest/sampling.py`'s `_is_test_file()` logic -- not a re-import of
   it, since that function takes a `Path` relative to the repo root and
   Chroma's returned metadata only carries the POSIX-style `filepath`
   string (see `indexer/store.py::_chunk_metadata`), and reaching into
   another component's private helper would break the "no cross-component
   direct imports" convention.
"""
from __future__ import annotations

import re

from viva.embedding_client import EmbeddingClient
from viva.indexer.store import VectorStore
from viva.questiongen.models import QuestionCategory, QuestionPlanItem

_CATEGORY_QUERY_TEMPLATES: dict[QuestionCategory, str] = {
    "architecture": "the overall architecture, design decisions, and how the major components fit together",
    "implementation_detail": "specific implementation details, algorithms, and logic",
    "tech_choice_rationale": "the choice of libraries, frameworks, or technologies and why they were used",
    "error_handling": "error handling, exception handling, input validation, and edge cases",
    "testing_strategy": "the testing strategy, test coverage, and how tests are structured",
}

# Mirrors ingest/sampling.py's _TEST_DIR_NAMES/_TEST_NAME_PATTERN -- see
# module docstring for why this isn't a direct import.
_TEST_DIR_NAMES = frozenset({"test", "tests", "__tests__", "spec", "specs"})
_TEST_NAME_PATTERN = re.compile(r"(^test_|_test\.|\.test\.|\.spec\.|^spec_|_spec\.)")

# Over-fetch factor for the test-path filter: retrieve more candidates
# than actually needed so filtering out test-path chunks still leaves
# `top_k` real results, rather than filtering an already-exact-sized set
# down below what the caller asked for.
_OVERFETCH_FACTOR = 3


def build_query(category: QuestionCategory, module_summary: str | None) -> str:
    """Expand a bare (category, module) pair into a richer retrieval
    query grounded in the module's own summary text (open question #6's
    query-reformulation fix). No LLM call -- deterministic and cheap,
    consistent with the codebase's existing template-based prompt
    construction (see `llm_client.py`'s `_build_prompt`)."""
    base = _CATEGORY_QUERY_TEMPLATES[category]
    if module_summary:
        return f"{base}. Context: {module_summary}"
    return base


def _is_test_path(filepath: str) -> bool:
    parts = filepath.split("/")
    if any(p.lower() in _TEST_DIR_NAMES for p in parts[:-1]):
        return True
    name = parts[-1].lower() if parts else ""
    return bool(_TEST_NAME_PATTERN.search(name))


def retrieve_grounding_chunks(
    plan_item: QuestionPlanItem,
    module_summary: str | None,
    vector_store: VectorStore,
    collection_name: str,
    embedding_client: EmbeddingClient,
    top_k: int,
) -> list[dict]:
    """Retrieve up to `top_k` grounding chunks for one plan item (FR13).

    Returns an empty list if the collection has nothing relevant --
    callers (`questiongen/__init__.py`) must treat that as "skip this
    plan item," never as an excuse to generate an ungrounded question.
    """
    query = build_query(plan_item.category, module_summary)
    [query_embedding] = embedding_client.embed([query])
    where = {"module": plan_item.target_module} if plan_item.target_module else None

    candidates = vector_store.query(
        collection_name, query_embedding, n_results=top_k * _OVERFETCH_FACTOR, where=where
    )

    if plan_item.category == "testing_strategy":
        # Prefer test chunks for this category rather than filtering them
        # out -- a stable sort keeps Chroma's original relevance order
        # within each group.
        candidates = sorted(
            candidates, key=lambda c: _is_test_path(c["metadata"]["filepath"]), reverse=True
        )
    else:
        filtered = [c for c in candidates if not _is_test_path(c["metadata"]["filepath"])]
        # Never let filtering leave a plan item with zero grounding when
        # the module's real code is thin and Chroma's only close matches
        # happened to be tests -- fall back to the unfiltered candidates
        # rather than incorrectly skipping the question entirely.
        candidates = filtered or candidates

    return candidates[:top_k]
