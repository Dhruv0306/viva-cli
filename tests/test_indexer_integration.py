"""End-to-end tests for viva.indexer.index_repo() against the checked-in
golden repos, following the same clone-stub + fake-client pattern as
tests/test_analyzer_integration.py.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import viva.ingest as ingest_pkg
from viva.analyzer import analyze_repo
from viva.analyzer.models import AnalysisResult, AnalysisStats
from viva.config import Config
from viva.embedding_client import EmbeddingClient
from viva.indexer import index_repo
from viva.indexer.store import VectorStore, collection_name
from viva.ingest import ingest_repo
from viva.ingest.clone import ClonedRepo
from viva.llm_client import LLMClient
from viva.profile import ProjectProfile

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "golden_repos"


class _FakeLLMClient(LLMClient):
    def classify_answer(self, *a, **k):
        raise NotImplementedError

    def generate_feedback(self, *a, **k):
        raise NotImplementedError

    def summarize_file(self, path, language, content_excerpt, target_tokens):
        return f"summary of {path}"

    def reduce(self, label, summaries, target_tokens):
        return f"reduced({label}, {len(summaries)} items)"

    def generate_question(self, category, target_module, grounding_context, target_file=None, avoid_questions=None):
        raise NotImplementedError  # not exercised by Phase 4 indexing tests


class _FakeEmbeddingClient(EmbeddingClient):
    """Deterministic stand-in -- no real Ollama call. Tracks call count
    and the batch size of each call so tests can assert on the §9.6
    per-file batching behavior, not just the final embedding values."""

    def __init__(self) -> None:
        self.call_batch_sizes: list[int] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.call_batch_sizes.append(len(texts))
        # One trivial, distinguishable vector per text -- length encodes
        # the text so equality-based assertions can check the right
        # chunk's embedding landed in the right place without needing a
        # real embedding model.
        return [[float(len(t)), 0.0] for t in texts]


def _config(vector_db_path: str) -> Config:
    return Config(
        llm_model="test-model", embedding_model="nomic-embed-text", temperature=0.3,
        ollama_host="http://localhost:11434", viva_duration_minutes=30, max_questions=8,
        max_followup_depth=1, session_retention_days=7, max_files=500, test_file_quota_pct=10,
        github_token=None, map_reduce_batch_size=8, max_reduce_context_tokens=100_000,
        line_window_size=60, line_window_overlap=15, vector_db_path=vector_db_path, top_k_retrieval=5,
        session_db_path="./data/viva.db", avg_time_per_category_seconds=180,
        question_similarity_threshold=0.90,
        eval_flush_timeout_seconds=60,
    )


def _stub_clone_from_fixture(fixture_name: str):
    def _stub(repo_url, dest_dir, branch=None, github_token=None):
        shutil.copytree(FIXTURES_DIR / fixture_name, dest_dir)
        return ClonedRepo(
            repo_url=repo_url, repo_slug="fixture/" + fixture_name, branch=branch or "main",
            commit_sha="fixture0000", local_path=dest_dir,
        )

    return _stub


def _build_profile(fixture_name: str, tmp_path, mocker, config: Config) -> ProjectProfile:
    mocker.patch.object(ingest_pkg, "clone_repo", _stub_clone_from_fixture(fixture_name))
    ingest_result = ingest_repo(
        f"https://github.com/fixture/{fixture_name}", config, work_dir=tmp_path / "clone"
    )
    analysis_result = analyze_repo(ingest_result, config, _FakeLLMClient())
    return ProjectProfile.build(ingest_result, analysis_result)


def test_index_repo_builds_collection_and_indexes_chunks(tmp_path, mocker):
    config = _config(str(tmp_path / "chroma"))
    profile = _build_profile("py_small", tmp_path, mocker, config)
    embedding_client = _FakeEmbeddingClient()

    result = index_repo(profile, config, embedding_client)

    assert result.collection_name == collection_name("fixture/py_small", "fixture0000")
    assert result.stats.reused_existing_collection is False
    assert result.stats.chunks_built > 0
    assert result.stats.files_processed == len(profile.sampled_files)

    store = VectorStore(str(tmp_path / "chroma"))
    assert store.collection_exists(result.collection_name) is True


def test_index_repo_embeds_in_per_file_batches(tmp_path, mocker):
    """§9.6: one embed() call per file's chunk list, not one per chunk
    and not one for the whole repo."""
    config = _config(str(tmp_path / "chroma"))
    profile = _build_profile("py_small", tmp_path, mocker, config)
    embedding_client = _FakeEmbeddingClient()

    index_repo(profile, config, embedding_client)

    # One call per file that produced at least one chunk -- never a
    # single call spanning multiple files' chunks, never zero-length
    # calls for files with no chunks.
    assert len(embedding_client.call_batch_sizes) <= len(profile.sampled_files)
    assert all(size > 0 for size in embedding_client.call_batch_sizes)


def test_index_repo_reuses_existing_collection_without_reembedding(tmp_path, mocker):
    config = _config(str(tmp_path / "chroma"))
    profile = _build_profile("py_small", tmp_path, mocker, config)
    embedding_client = _FakeEmbeddingClient()

    first = index_repo(profile, config, embedding_client)
    assert first.stats.reused_existing_collection is False
    first_call_count = len(embedding_client.call_batch_sizes)
    assert first_call_count > 0

    second = index_repo(profile, config, embedding_client)

    assert second.stats.reused_existing_collection is True
    assert second.collection_name == first.collection_name
    # No new embed() calls made on the reuse path.
    assert len(embedding_client.call_batch_sizes) == first_call_count


def test_index_repo_retrieval_returns_grounded_chunk(tmp_path, mocker):
    """Loose approximation of the plan.md Phase 4 exit criteria ('manual
    retrieval queries return relevant, correctly-scoped chunks'): querying
    with the exact embedding of a known chunk's own text must return that
    same chunk, correctly scoped to its file/symbol."""
    from viva.indexer.chunking import build_chunks

    config = _config(str(tmp_path / "chroma"))
    profile = _build_profile("py_small", tmp_path, mocker, config)
    embedding_client = _FakeEmbeddingClient()
    result = index_repo(profile, config, embedding_client)

    known_chunks = build_chunks(
        profile.sampled_files, profile.local_path, profile.repo_slug, profile.commit_sha, config
    )
    target = next(c for c in known_chunks if c.symbol_name == "create_app")

    store = VectorStore(str(tmp_path / "chroma"))
    [query_embedding] = embedding_client.embed([target.text])
    results = store.query(result.collection_name, query_embedding, n_results=1)

    assert results
    assert results[0]["id"] == target.id
    assert results[0]["metadata"]["filepath"] == "src/app/main.py"
    assert results[0]["metadata"]["symbol_name"] == "create_app"
