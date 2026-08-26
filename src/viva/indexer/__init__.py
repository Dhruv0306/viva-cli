"""Indexer/RAG component (docs/plan.md Phase 4, docs/design.md §1
"Indexer/RAG").

Chunk (FR9, `chunking.py`) -> embed (FR10, `viva.embedding_client`) ->
store in Chroma (FR10/FR11, `store.py`). Public entrypoint
`index_repo()` lands in a later patch of this series once the vector
store wrapper exists -- see
docs/system-design/09-phase-4-indexing-design.md for the full design.
"""
from __future__ import annotations
