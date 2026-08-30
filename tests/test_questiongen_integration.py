"""End-to-end tests for viva.questiongen.generate_all() against the
checked-in golden repos, following the same clone-stub + fake-client
pattern as tests/test_indexer_integration.py.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import viva.ingest as ingest_pkg
from viva.analyzer import analyze_repo
from viva.config import Config
from viva.embedding_client import EmbeddingClient
from viva.indexer import index_repo
from viva.indexer.store import VectorStore
from viva.ingest import ingest_repo
from viva.ingest.clone import ClonedRepo
from viva.llm_client import LLMClient
from viva.profile import ProjectProfile
from viva.questiongen import generate_all

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "golden_repos"


class _FakeLLMClient(LLMClient):
    def evaluate_answer(self, *a, **k):
        raise NotImplementedError

    def summarize_file(self, path, language, content_excerpt, target_tokens):
        return f"summary of {path}"

    def reduce(self, label, summaries, target_tokens):
        return f"reduced({label}, {len(summaries)} items)"

    def generate_question(self, category, target_module, grounding_context, target_file=None):
        target = target_file or target_module or "the project"
        return f"Question about {category} in {target}?"


class _FakeEmbeddingClient(EmbeddingClient):
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t)), 0.0] for t in texts]


def _config(vector_db_path: str, max_questions: int = 8) -> Config:
    return Config(
        llm_model="test-model", embedding_model="nomic-embed-text", temperature=0.3,
        ollama_host="http://localhost:11434", viva_duration_minutes=30, max_questions=max_questions,
        max_followup_depth=1, session_retention_days=7, max_files=500, test_file_quota_pct=10,
        github_token=None, map_reduce_batch_size=8, max_reduce_context_tokens=100_000,
        line_window_size=60, line_window_overlap=15, vector_db_path=vector_db_path, top_k_retrieval=5,
        session_db_path="./data/viva.db", avg_time_per_category_seconds=180,
        question_similarity_threshold=0.90,
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


def test_generate_all_produces_grounded_questions_across_categories(tmp_path, mocker):
    config = _config(str(tmp_path / "chroma"))
    profile = _build_profile("py_small", tmp_path, mocker, config)
    embedding_client = _FakeEmbeddingClient()
    llm_client = _FakeLLMClient()

    index_result = index_repo(profile, config, embedding_client)
    store = VectorStore(str(tmp_path / "chroma"))

    questions, stats = generate_all(
        profile, config, store, index_result.collection_name, embedding_client, llm_client
    )

    assert stats.plan_items_built > 0
    assert stats.questions_generated + stats.plan_items_skipped_no_grounding == stats.plan_items_built
    assert len(questions) == stats.questions_generated
    for q in questions:
        # FR13: never ungrounded.
        assert len(q.grounding_chunk_ids) > 0
        assert q.question_text


def test_generate_all_skips_items_with_no_grounding(tmp_path, mocker):
    # An empty collection (no indexing run) means every plan item should
    # be skipped rather than a question fabricated ungrounded.
    config = _config(str(tmp_path / "chroma"))
    profile = _build_profile("py_small", tmp_path, mocker, config)
    embedding_client = _FakeEmbeddingClient()
    llm_client = _FakeLLMClient()

    empty_store = VectorStore(str(tmp_path / "chroma_empty"))
    # An existing-but-empty collection (not an unindexed repo -- Chroma
    # raises on a genuinely missing collection, which isn't the scenario
    # under test here): every retrieval query must come back empty.
    empty_store._client.get_or_create_collection("empty-collection")

    questions, stats = generate_all(
        profile, config, empty_store, "empty-collection", embedding_client, llm_client
    )

    assert questions == []
    assert stats.questions_generated == 0
    assert stats.plan_items_skipped_no_grounding == stats.plan_items_built
