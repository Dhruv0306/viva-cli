"""FR10/FR11: Chroma-backed vector store wrapper.

`VectorStore` is what NFR5 means for the vector-store half of "LLM
backend and vector store must sit behind thin interfaces" -- Chroma is
the only implementation for v1, but nothing outside this module should
import `chromadb` directly.

**Collection keying:** `{repo_slug}-{commit_sha}`, exactly as
docs/design.md §8 / 05-repo-lifecycle-and-language-coverage.md §5.2
specify. `repo_slug` (`owner/repo`, see `ingest/clone.py`) contains a
`/`, which Chroma's collection-name charset (`[a-zA-Z0-9._-]`, 3-512
chars, must start/end alphanumeric) rejects outright -- `collection_name()`
sanitizes it. This only affects the *name* Chroma sees; chunk IDs
(`models.py`) have no such charset restriction and keep the raw slug for
readability/debuggability.
"""
from __future__ import annotations

import re

import chromadb

from viva.indexer.models import Chunk

_INVALID_COLLECTION_NAME_CHARS = re.compile(r"[^a-zA-Z0-9._-]")


def collection_name(repo_slug: str, commit_sha: str) -> str:
    sanitized_slug = _INVALID_COLLECTION_NAME_CHARS.sub("--", repo_slug)
    return f"{sanitized_slug}-{commit_sha}"


class VectorStore:
    def __init__(self, path: str) -> None:
        self._client = chromadb.PersistentClient(path=path)

    def collection_exists(self, name: str) -> bool:
        """Used by indexer/__init__.py to implement the reuse decision
        in docs/system-design/09-phase-4-indexing-design.md §9.4: a
        collection already existing under this exact
        {repo_slug}-{commit_sha} key means an unchanged commit was
        already indexed, so re-embedding is skipped entirely."""
        return any(c.name == name for c in self._client.list_collections())

    def upsert_chunks(self, name: str, chunks: list[Chunk], embeddings: list[list[float]]) -> None:
        if not chunks:
            return
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"chunks ({len(chunks)}) and embeddings ({len(embeddings)}) count mismatch"
            )
        collection = self._client.get_or_create_collection(name)
        collection.upsert(
            ids=[c.id for c in chunks],
            embeddings=embeddings,
            documents=[c.text for c in chunks],
            metadatas=[_chunk_metadata(c) for c in chunks],
        )

    def query(
        self,
        name: str,
        query_embedding: list[float],
        n_results: int,
        where: dict | None = None,
    ) -> list[dict]:
        """Semantic similarity, optionally narrowed by an exact-match
        metadata filter (FR11's "chunks belonging to module X" --
        `where={"module": "payments"}`), via Chroma's native `where`.
        """
        collection = self._client.get_collection(name)
        result = collection.query(
            query_embeddings=[query_embedding], n_results=n_results, where=where
        )
        # Chroma returns one outer list per query embedding; callers here
        # only ever pass one, so flatten to a single list of results.
        ids = result["ids"][0]
        documents = result["documents"][0]
        metadatas = result["metadatas"][0]
        distances = result["distances"][0]
        return [
            {"id": cid, "text": doc, "metadata": meta, "distance": dist}
            for cid, doc, meta, dist in zip(ids, documents, metadatas, distances)
        ]


def _chunk_metadata(chunk: Chunk) -> dict:
    # Chroma metadata values must be str/int/float/bool -- None isn't
    # accepted, so symbol_name/language (both legitimately None for some
    # chunks, see models.py) get an empty-string sentinel here rather
    # than propagating None into the upsert call.
    return {
        "filepath": chunk.filepath,
        "module": chunk.module,
        "symbol_name": chunk.symbol_name or "",
        "kind": chunk.kind,
        "parse_method": chunk.parse_method,
        "language": chunk.language or "",
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
    }
