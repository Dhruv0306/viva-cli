from __future__ import annotations

from unittest.mock import MagicMock

from viva.questiongen.models import QuestionPlanItem
from viva.questiongen.retrieval import _is_test_path, build_query, retrieve_grounding_chunks


def test_build_query_includes_module_summary_when_present():
    query = build_query("error_handling", "Handles payment webhooks and retries.")
    assert "error handling" in query.lower()
    assert "Handles payment webhooks and retries." in query


def test_build_query_falls_back_to_template_without_module_summary():
    query = build_query("architecture", None)
    assert "architecture" in query.lower()
    assert "Context:" not in query


def test_build_query_anchors_to_target_file_when_present():
    query = build_query("implementation_detail", "auth module summary", "auth/handler.py")
    assert "auth/handler.py" in query


def test_is_test_path_detects_test_directories_and_filenames():
    assert _is_test_path("tests/test_handler.py") is True
    assert _is_test_path("src/tests/handler.py") is True
    assert _is_test_path("src/handler_test.go") is True
    assert _is_test_path("src/payments/handler.py") is False


def _chunk(cid: str, filepath: str) -> dict:
    return {
        "id": cid,
        "text": f"body of {cid}",
        "metadata": {"filepath": filepath, "start_line": 1, "end_line": 2},
        "distance": 0.1,
    }


def test_retrieve_grounding_chunks_filters_test_paths_for_non_testing_category():
    plan_item = QuestionPlanItem(id="q_01", category="implementation_detail", target_module="auth")
    store = MagicMock()
    store.query.return_value = [
        _chunk("c1", "src/auth/handler.py"),
        _chunk("c2", "tests/test_auth.py"),
        _chunk("c3", "src/auth/session.py"),
    ]
    embedding_client = MagicMock()
    embedding_client.embed.return_value = [[0.1, 0.2]]

    results = retrieve_grounding_chunks(
        plan_item, "auth module summary", store, "collection", embedding_client, top_k=5
    )

    ids = [r["id"] for r in results]
    assert "c2" not in ids
    assert set(ids) == {"c1", "c3"}


def test_retrieve_grounding_chunks_prefers_test_paths_for_testing_strategy():
    plan_item = QuestionPlanItem(id="q_01", category="testing_strategy", target_module="auth")
    store = MagicMock()
    store.query.return_value = [
        _chunk("c1", "src/auth/handler.py"),
        _chunk("c2", "tests/test_auth.py"),
    ]
    embedding_client = MagicMock()
    embedding_client.embed.return_value = [[0.1, 0.2]]

    results = retrieve_grounding_chunks(
        plan_item, "auth module summary", store, "collection", embedding_client, top_k=5
    )

    assert results[0]["id"] == "c2"


def test_retrieve_grounding_chunks_falls_back_to_unfiltered_when_all_are_tests():
    # A thin module where every close match happens to be a test file --
    # filtering must not zero out the result entirely.
    plan_item = QuestionPlanItem(id="q_01", category="error_handling", target_module="thin")
    store = MagicMock()
    store.query.return_value = [_chunk("c1", "tests/test_thin.py")]
    embedding_client = MagicMock()
    embedding_client.embed.return_value = [[0.1, 0.2]]

    results = retrieve_grounding_chunks(
        plan_item, "thin module summary", store, "collection", embedding_client, top_k=5
    )

    assert len(results) == 1
    assert results[0]["id"] == "c1"


def test_retrieve_grounding_chunks_uses_module_metadata_filter():
    plan_item = QuestionPlanItem(id="q_01", category="architecture", target_module=None)
    store = MagicMock()
    store.query.return_value = []
    embedding_client = MagicMock()
    embedding_client.embed.return_value = [[0.1, 0.2]]

    retrieve_grounding_chunks(plan_item, "arch summary", store, "collection", embedding_client, top_k=5)

    assert store.query.call_args.kwargs["where"] is None

    plan_item_scoped = QuestionPlanItem(id="q_02", category="implementation_detail", target_module="auth")
    retrieve_grounding_chunks(
        plan_item_scoped, "auth summary", store, "collection", embedding_client, top_k=5
    )
    assert store.query.call_args.kwargs["where"] == {"module": "auth"}


def test_retrieve_grounding_chunks_filters_by_filepath_for_file_level_items():
    plan_item = QuestionPlanItem(
        id="q_01", category="implementation_detail", target_module="src", target_file="src/core.py"
    )
    store = MagicMock()
    store.query.return_value = [_chunk("c1", "src/core.py")]
    embedding_client = MagicMock()
    embedding_client.embed.return_value = [[0.1, 0.2]]

    retrieve_grounding_chunks(plan_item, "src summary", store, "collection", embedding_client, top_k=5)

    # File-level items narrow to the exact file, not the whole module.
    assert store.query.call_args.kwargs["where"] == {"filepath": "src/core.py"}
