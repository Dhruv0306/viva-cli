"""Indexer/RAG component (docs/plan.md Phase 4, docs/design.md §1
"Indexer/RAG").

Chunk (FR9, `chunking.py`) -> embed (FR10, `viva.embedding_client`) ->
store in Chroma (FR10/FR11, `store.py`), keyed
`{repo_slug}-{commit_sha}` (design.md §8) with reuse: if a collection
already exists for this exact commit, indexing is a no-op past the
existence check (docs/system-design/09-phase-4-indexing-design.md §9.4)
-- an unchanged repo/commit never pays to re-parse, re-embed, or re-store.

Public entrypoint: `index_repo()`. Per design.md's component rule ("no
service calls another directly"), this is the seam the future
Orchestrator (Phase 6) will call -- everything else in this package is
an internal implementation detail.
"""
from __future__ import annotations

from itertools import groupby

from viva.config import Config
from viva.embedding_client import EmbeddingClient
from viva.indexer.chunking import build_chunks
from viva.indexer.models import Chunk, IndexResult, IndexStats
from viva.indexer.store import VectorStore, collection_name
from viva.profile import ProjectProfile

__all__ = ["index_repo"]


def index_repo(
    profile: ProjectProfile,
    config: Config,
    embedding_client: EmbeddingClient,
    vector_store: VectorStore | None = None,
) -> IndexResult:
    """Run the full Indexer pipeline over a `ProjectProfile` (FR9-FR11).

    Reads file contents from `profile.local_path` for every
    `profile.sampled_files` entry, so -- like `analyze_repo()` -- this
    must run before the raw clone is deleted (NFR7 / design.md §8.2).

    `vector_store` is injectable for testing; defaults to a real
    `VectorStore` at `config.vector_db_path`.
    """
    store = vector_store or VectorStore(config.vector_db_path)
    name = collection_name(profile.repo_slug, profile.commit_sha)

    if store.collection_exists(name):
        # design.md §8's stated rationale for commit-SHA keying is
        # "enables reuse without re-indexing an unchanged repo" -- this
        # is what actually makes that true rather than just detecting
        # staleness. No re-parse, no re-embed call, no re-store.
        return IndexResult(
            collection_name=name,
            stats=IndexStats(files_processed=0, chunks_built=0, reused_existing_collection=True),
        )

    chunks = build_chunks(
        sampled_files=profile.sampled_files,
        root=profile.local_path,
        repo_slug=profile.repo_slug,
        commit_sha=profile.commit_sha,
        config=config,
    )

    embeddings = _embed_per_file(chunks, embedding_client)
    store.upsert_chunks(name, chunks, embeddings)

    return IndexResult(
        collection_name=name,
        stats=IndexStats(
            files_processed=len(profile.sampled_files),
            chunks_built=len(chunks),
            reused_existing_collection=False,
        ),
    )


def _embed_per_file(chunks: list[Chunk], embedding_client: EmbeddingClient) -> list[list[float]]:
    """One embed() call per file's full chunk list, not one call per
    chunk and not one call for the whole repo -- keeps the Ollama call
    count proportional to files analyzed (same order of magnitude as
    Phase 3's one-summarize_file-call-per-file Map step) rather than to
    chunk count, per §9.6. Relies on chunking.build_chunks()'s documented
    guarantee that same-file chunks are contiguous, so a plain
    itertools.groupby (no re-sort) is enough to recover file boundaries.
    """
    embeddings: list[list[float]] = []
    for _filepath, group in groupby(chunks, key=lambda c: c.filepath):
        file_chunks = list(group)
        embeddings.extend(embedding_client.embed([c.text for c in file_chunks]))
    return embeddings
