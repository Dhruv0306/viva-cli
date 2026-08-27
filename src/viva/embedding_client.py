"""Embedding client interface and the Ollama implementation (FR10).

Per docs/design.md §10 / NFR5: a thin interface, mirroring `LLMClient`'s
shape, so nothing else in the pipeline imports Ollama's embedding API
directly. Kept as its own interface rather than folded into `LLMClient`
-- embedding (`/api/embed`) and chat completion (`/api/chat`) are
different Ollama endpoints with different failure modes and different
call shapes (batch-of-texts-in/batch-of-vectors-out vs. one structured
turn), and conflating them would make `LLMClient` harder to fake in
tests that only need one half. See
docs/system-design/09-phase-4-indexing-design.md §9.3.
"""
from __future__ import annotations

import abc

import ollama


class EmbeddingClient(abc.ABC):
    """Thin interface so pipeline code never imports Ollama's embedding
    API directly (NFR5)."""

    @abc.abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, returning one vector per input in the
        same order as `texts`.

        Batched rather than one-call-per-text: Ollama's `/api/embed`
        accepts a batch natively, and callers (indexer/__init__.py) batch
        by file so the call count stays proportional to files analyzed
        rather than to chunk count -- the same order-of-magnitude
        tradeoff Phase 3's one-`summarize_file`-call-per-file Map step
        already made.
        """
        raise NotImplementedError


class OllamaEmbeddingClient(EmbeddingClient):
    def __init__(self, model: str, host: str, timeout: float | None = 60.0) -> None:
        self._model = model
        self._client = ollama.Client(host=host, timeout=timeout)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._client.embed(model=self._model, input=texts)
        return [list(vector) for vector in response["embeddings"]]
