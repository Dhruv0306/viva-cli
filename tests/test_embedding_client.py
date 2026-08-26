from unittest.mock import MagicMock

import pytest

from viva.embedding_client import OllamaEmbeddingClient


def _embed_response(vectors: list[list[float]]) -> dict:
    return {"embeddings": vectors}


@pytest.fixture
def client():
    c = OllamaEmbeddingClient(model="nomic-embed-text", host="http://localhost:11434")
    c._client = MagicMock()  # replace the real ollama.Client, no network involved
    return c


def test_embed_returns_one_vector_per_text(client):
    client._client.embed.return_value = _embed_response([[0.1, 0.2], [0.3, 0.4]])

    result = client.embed(["def foo(): ...", "class Bar: ..."])

    assert result == [[0.1, 0.2], [0.3, 0.4]]
    client._client.embed.assert_called_once_with(
        model="nomic-embed-text", input=["def foo(): ...", "class Bar: ..."]
    )


def test_embed_empty_list_short_circuits_without_a_call(client):
    result = client.embed([])

    assert result == []
    client._client.embed.assert_not_called()


def test_embed_preserves_input_order(client):
    client._client.embed.return_value = _embed_response([[1.0], [2.0], [3.0]])

    result = client.embed(["a", "b", "c"])

    assert result == [[1.0], [2.0], [3.0]]
