"""Tests for viva.indexer.store against a real Chroma PersistentClient
pointed at a tmp_path -- Chroma is file-backed and needs no network, so
there's no need to mock it (same "test real behavior where it's cheap"
approach the golden-repo integration tests already use)."""
from __future__ import annotations

import pytest

from viva.indexer.models import Chunk
from viva.indexer.store import VectorStore, collection_name


def _chunk(id: str, text: str, module: str = "src", symbol_name: str | None = "foo") -> Chunk:
    return Chunk(
        id=id, text=text, filepath="src/app/main.py", module=module, symbol_name=symbol_name,
        kind="function" if symbol_name else "line_window",
        parse_method="ast" if symbol_name else "line_window",
        language="python", start_line=1, end_line=3,
    )


def test_collection_name_sanitizes_repo_slug_slash():
    name = collection_name("owner/repo", "abc123def456")
    assert name == "owner--repo-abc123def456"
    # Must satisfy Chroma's own charset before it's ever handed to
    # chromadb.PersistentClient.create_collection.
    assert all(c.isalnum() or c in "._-" for c in name)


def test_collection_does_not_exist_initially(tmp_path):
    store = VectorStore(str(tmp_path / "chroma"))
    assert store.collection_exists("owner--repo-abc123def456") is False


def test_upsert_then_collection_exists(tmp_path):
    store = VectorStore(str(tmp_path / "chroma"))
    name = collection_name("owner/repo", "abc123def456")

    store.upsert_chunks(name, [_chunk("c1", "def foo(): ...")], [[0.1, 0.2, 0.3]])

    assert store.collection_exists(name) is True


def test_upsert_empty_chunks_is_a_noop(tmp_path):
    store = VectorStore(str(tmp_path / "chroma"))
    name = collection_name("owner/repo", "abc123def456")

    store.upsert_chunks(name, [], [])

    assert store.collection_exists(name) is False


def test_upsert_mismatched_lengths_raises(tmp_path):
    store = VectorStore(str(tmp_path / "chroma"))
    name = collection_name("owner/repo", "abc123def456")

    with pytest.raises(ValueError, match="mismatch"):
        store.upsert_chunks(name, [_chunk("c1", "def foo(): ...")], [])


def test_query_returns_closest_match_with_metadata(tmp_path):
    store = VectorStore(str(tmp_path / "chroma"))
    name = collection_name("owner/repo", "abc123def456")
    store.upsert_chunks(
        name,
        [_chunk("c1", "def foo(): ..."), _chunk("c2", "def bar(): ...", symbol_name="bar")],
        [[1.0, 0.0], [0.0, 1.0]],
    )

    results = store.query(name, query_embedding=[1.0, 0.0], n_results=1)

    assert len(results) == 1
    assert results[0]["id"] == "c1"
    assert results[0]["metadata"]["symbol_name"] == "foo"
    assert results[0]["metadata"]["filepath"] == "src/app/main.py"
    assert results[0]["distance"] == pytest.approx(0.0, abs=1e-6)


def test_query_with_module_metadata_filter(tmp_path):
    store = VectorStore(str(tmp_path / "chroma"))
    name = collection_name("owner/repo", "abc123def456")
    store.upsert_chunks(
        name,
        [_chunk("c1", "def foo(): ...", module="auth"), _chunk("c2", "def bar(): ...", module="billing", symbol_name="bar")],
        [[1.0, 0.0], [1.0, 0.0]],  # identical embeddings -- filter is what should distinguish them
    )

    results = store.query(name, query_embedding=[1.0, 0.0], n_results=5, where={"module": "billing"})

    assert len(results) == 1
    assert results[0]["id"] == "c2"


def test_chunk_metadata_never_contains_none_for_optional_fields(tmp_path):
    """Chroma rejects None metadata values -- symbol_name/language must
    be sanitized to '' rather than propagated as None (see
    store._chunk_metadata)."""
    store = VectorStore(str(tmp_path / "chroma"))
    name = collection_name("owner/repo", "abc123def456")
    line_window_chunk = Chunk(
        id="c1", text="some raw lines", filepath="README.md", module="", symbol_name=None,
        kind="line_window", parse_method="line_window", language=None, start_line=1, end_line=10,
    )

    # Must not raise -- would if None ever reached chromadb's upsert call.
    store.upsert_chunks(name, [line_window_chunk], [[0.5, 0.5]])

    results = store.query(name, query_embedding=[0.5, 0.5], n_results=1)
    assert results[0]["metadata"]["symbol_name"] == ""
    assert results[0]["metadata"]["language"] == ""


def test_get_by_ids_returns_exact_chunks_in_no_particular_order(tmp_path):
    store = VectorStore(str(tmp_path / "chroma"))
    name = collection_name("owner/repo", "abc123def456")
    store.upsert_chunks(
        name,
        [_chunk("c1", "def foo(): ..."), _chunk("c2", "def bar(): ...", symbol_name="bar")],
        [[1.0, 0.0], [0.0, 1.0]],
    )

    results = store.get_by_ids(name, ["c2", "c1"])

    assert {r["id"] for r in results} == {"c1", "c2"}
    by_id = {r["id"]: r for r in results}
    assert by_id["c1"]["text"] == "def foo(): ..."
    assert by_id["c2"]["metadata"]["symbol_name"] == "bar"
    # Not a similarity search -- no 'distance' key in the result shape.
    assert "distance" not in by_id["c1"]


def test_get_by_ids_empty_list_returns_empty_without_touching_chroma(tmp_path):
    store = VectorStore(str(tmp_path / "chroma"))
    name = collection_name("owner/repo", "abc123def456")

    # No collection created at all -- must not raise on an empty request.
    assert store.get_by_ids(name, []) == []


def test_get_by_ids_missing_collection_returns_empty_not_raises(tmp_path):
    store = VectorStore(str(tmp_path / "chroma"))
    name = collection_name("owner/repo", "does-not-exist")

    assert store.get_by_ids(name, ["c1"]) == []


def test_get_by_ids_some_ids_missing_from_existing_collection(tmp_path):
    store = VectorStore(str(tmp_path / "chroma"))
    name = collection_name("owner/repo", "abc123def456")
    store.upsert_chunks(name, [_chunk("c1", "def foo(): ...")], [[1.0, 0.0]])

    # c2 was never indexed -- silently omitted, not an error, per
    # get_by_ids's "some grounding chunks are gone" degrade-gracefully
    # contract.
    results = store.get_by_ids(name, ["c1", "c2"])

    assert [r["id"] for r in results] == ["c1"]
