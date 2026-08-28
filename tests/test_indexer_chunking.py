"""Tests for viva.indexer.chunking.build_chunks() (FR9) against the
checked-in golden repos, following the same clone-stub pattern as
tests/test_analyzer_integration.py.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import viva.ingest as ingest_pkg
from viva.config import Config
from viva.indexer.chunking import build_chunks
from viva.ingest import ingest_repo
from viva.ingest.clone import ClonedRepo

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "golden_repos"


def _config(line_window_size: int = 60, line_window_overlap: int = 15) -> Config:
    return Config(
        llm_model="test-model", embedding_model="nomic-embed-text", temperature=0.3,
        ollama_host="http://localhost:11434", viva_duration_minutes=30, max_questions=8,
        max_followup_depth=1, session_retention_days=7, max_files=500, test_file_quota_pct=10,
        github_token=None, map_reduce_batch_size=8, max_reduce_context_tokens=None,
        line_window_size=line_window_size, line_window_overlap=line_window_overlap,
        vector_db_path="./data/chroma", top_k_retrieval=5,
        session_db_path="./data/viva.db", avg_time_per_category_seconds=180,
    )


def _stub_clone_from_fixture(fixture_name: str):
    def _stub(repo_url, dest_dir, branch=None, github_token=None):
        shutil.copytree(FIXTURES_DIR / fixture_name, dest_dir)
        return ClonedRepo(
            repo_url=repo_url, repo_slug="fixture/" + fixture_name, branch=branch or "main",
            commit_sha="fixture0000", local_path=dest_dir,
        )

    return _stub


def _ingest_fixture(fixture_name: str, tmp_path, mocker, config: Config | None = None):
    mocker.patch.object(ingest_pkg, "clone_repo", _stub_clone_from_fixture(fixture_name))
    return ingest_repo(
        f"https://github.com/fixture/{fixture_name}", config or _config(), work_dir=tmp_path / "clone"
    )


def test_build_chunks_produces_one_chunk_per_function(tmp_path, mocker):
    ingest_result = _ingest_fixture("py_small", tmp_path, mocker)

    chunks = build_chunks(
        ingest_result.sampled_files, ingest_result.local_path, "fixture/py_small", "fixture0000", _config()
    )

    main_chunks = [c for c in chunks if c.filepath == "src/app/main.py"]
    assert len(main_chunks) == 1
    chunk = main_chunks[0]
    assert chunk.symbol_name == "create_app"
    assert chunk.kind == "function"
    assert chunk.parse_method == "ast"
    assert chunk.module == "src"
    assert "def create_app" in chunk.text
    assert "return conn" in chunk.text


def test_build_chunks_ast_chunk_text_is_not_truncated(tmp_path, mocker):
    """Regression guard for docs/system-design/09-phase-4-indexing-design.md
    §9.1: chunk text must be the real full unit text, not
    CodeUnit.body_excerpt's 800-char/docstring-conditional excerpt."""
    ingest_result = _ingest_fixture("py_small", tmp_path, mocker)

    chunks = build_chunks(
        ingest_result.sampled_files, ingest_result.local_path, "fixture/py_small", "fixture0000", _config()
    )

    handler_chunks = [c for c in chunks if c.filepath == "src/app/auth/handlers.py"]
    assert handler_chunks
    for chunk in handler_chunks:
        # The full function text (signature through its closing line),
        # not just the first line -- distinguishes a real slice from a
        # signature-only or 800-char-capped excerpt.
        assert chunk.text.count("\n") >= (chunk.end_line - chunk.start_line)


def test_build_chunks_ids_are_stable_and_unique(tmp_path, mocker):
    ingest_result = _ingest_fixture("py_small", tmp_path, mocker)

    chunks = build_chunks(
        ingest_result.sampled_files, ingest_result.local_path, "fixture/py_small", "fixture0000", _config()
    )

    ids = [c.id for c in chunks]
    assert len(ids) == len(set(ids))
    assert all(cid.startswith("fixture/py_small-fixture0000-") for cid in ids)


def test_build_chunks_same_file_chunks_stay_contiguous(tmp_path, mocker):
    """chunking.py's contiguity guarantee (relied on by
    indexer/__init__.py's per-file embedding batching, §9.6)."""
    ingest_result = _ingest_fixture("py_small", tmp_path, mocker)

    chunks = build_chunks(
        ingest_result.sampled_files, ingest_result.local_path, "fixture/py_small", "fixture0000", _config()
    )

    seen_files: list[str] = []
    for chunk in chunks:
        if not seen_files or seen_files[-1] != chunk.filepath:
            assert chunk.filepath not in seen_files, (
                f"{chunk.filepath} chunks are not contiguous in the result"
            )
            seen_files.append(chunk.filepath)


def test_build_chunks_line_window_fallback_gets_real_line_ranges(tmp_path, mocker):
    # An artificially tiny window size forces every file into line_window
    # fallback isn't needed here -- js_small already includes non-Python
    # content that may not be covered depending on the language allowlist,
    # but the simplest deterministic way to force fallback is a made-up
    # extension. Reuse py_small and just check any line_window chunks
    # that occur naturally (e.g. from README.md, not in the AST allowlist)
    # have a sane, non-overlapping-with-itself line range.
    ingest_result = _ingest_fixture("py_small", tmp_path, mocker)

    chunks = build_chunks(
        ingest_result.sampled_files, ingest_result.local_path, "fixture/py_small", "fixture0000", _config()
    )

    line_window_chunks = [c for c in chunks if c.parse_method == "line_window"]
    assert line_window_chunks, "expected at least one non-Python/non-parseable file to fall back"
    for chunk in line_window_chunks:
        assert chunk.symbol_name is None
        assert chunk.kind == "line_window"
        assert chunk.start_line >= 1
        assert chunk.end_line >= chunk.start_line
