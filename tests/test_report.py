"""Tests for viva.report -- pure-dataclass fixtures, no DB/LLM, per
docs/system-design/13-phase-8-report-design.md §13.2/§13.8.
"""
from __future__ import annotations

import json

from viva.report import ReportBuilder, render_json, render_markdown
from viva.schemas import EvaluationRecord, MissedPoint
from viva.storage.session_store import QARecordRow, SessionRecord


def _session(**overrides) -> SessionRecord:
    values = dict(
        session_id="sess-1",
        repo_url="https://github.com/owner/repo",
        repo_slug="owner/repo",
        commit_sha="abc123def456",
        branch="main",
        session_name=None,
        status="COMPLETE",
        duration_seconds=1800,
        collection_name="owner-repo-abc123",
        profile_path="/tmp/profile.json",
        error_message=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:30:00+00:00",
    )
    values.update(overrides)
    return SessionRecord(**values)


def _qa(question_id: str, category: str, status: str, eval_record: EvaluationRecord | None, **overrides) -> QARecordRow:
    values = dict(
        session_id="sess-1",
        question_id=question_id,
        category=category,
        target_module=None,
        target_file=None,
        is_followup_of=None,
        question_text=f"Question {question_id}?",
        grounding_chunk_ids=[],
        status=status,
        answer_text="an answer" if status == "answered" else None,
        asked_at="2026-01-01T00:05:00+00:00" if status != "pending" else None,
        answered_at="2026-01-01T00:06:00+00:00" if status == "answered" else None,
        eval_status="complete" if eval_record else "deferred",
        eval_json=eval_record.model_dump_json() if eval_record else None,
    )
    values.update(overrides)
    return QARecordRow(**values)


def _record(classification: str, **overrides) -> EvaluationRecord:
    values = dict(
        classification=classification,
        summary="A verdict summary.",
        cited_file="src/module.py",
        did_well=[],
        missed=[],
        did_wrong=[],
        improvement="Study X more.",
        needs_review=False,
    )
    values.update(overrides)
    return EvaluationRecord(**values)


def test_build_counts_only_answered_records():
    session = _session()
    qa_records = [
        _qa("q1", "architecture", "answered", _record("correct", did_well=["Explained the state machine well."])),
        _qa("q2", "testing", "pending", None, question_text=None),
        _qa("q3", "testing", "skipped_no_grounding", None, question_text=None),
    ]

    report = ReportBuilder().build(session, qa_records)

    assert report.total_questions == 3
    assert report.answered_count == 1
    assert report.classification_counts == {"correct": 1}


def test_strengths_pulled_from_correct_and_partial():
    session = _session()
    qa_records = [
        _qa("q1", "architecture", "answered", _record("correct", did_well=["Clear on the Orchestrator boundary."])),
        _qa("q2", "testing", "answered", _record("partial", did_well=["Knew the fixture repos used."])),
        _qa("q3", "rag", "answered", _record("incorrect", did_well=["Should not appear."])),
    ]

    report = ReportBuilder().build(session, qa_records)

    assert "Clear on the Orchestrator boundary." in report.strengths
    assert "Knew the fixture repos used." in report.strengths
    assert "Should not appear." not in report.strengths


def test_weaknesses_pulled_from_partial_and_incorrect_missed_and_did_wrong():
    session = _session()
    qa_records = [
        _qa(
            "q1",
            "rag",
            "answered",
            _record(
                "partial",
                missed=[MissedPoint(point="Didn't mention Chroma collection keying", cited_file="src/viva/indexer/store.py")],
            ),
        ),
        _qa(
            "q2",
            "evaluation",
            "answered",
            _record(
                "incorrect",
                did_wrong=[MissedPoint(point="Claimed evaluation runs synchronously", cited_file=None)],
            ),
        ),
        _qa("q3", "ingestion", "answered", _record("correct", did_well=["Fine."])),
    ]

    report = ReportBuilder().build(session, qa_records)

    assert "Didn't mention Chroma collection keying (src/viva/indexer/store.py)" in report.weaknesses
    assert "Claimed evaluation runs synchronously" in report.weaknesses


def test_topics_to_revisit_ranked_by_frequency_then_first_appearance():
    session = _session()
    qa_records = [
        _qa("q1", "rag", "answered", _record("partial")),
        _qa("q2", "evaluation", "answered", _record("incorrect")),
        _qa("q3", "rag", "answered", _record("incorrect")),
        _qa("q4", "architecture", "answered", _record("correct")),
    ]

    report = ReportBuilder().build(session, qa_records)

    # "rag" appears twice, "evaluation" once, "architecture" not at all
    # (never partial/incorrect) -- rag first, then evaluation.
    assert report.topics_to_revisit == ["rag", "evaluation"]


def test_needs_review_excluded_from_rollups_but_counted_and_listed():
    session = _session()
    qa_records = [
        _qa(
            "q1",
            "rag",
            "answered",
            _record(
                "partial",
                needs_review=True,
                missed=[MissedPoint(point="Should not leak into weaknesses", cited_file=None)],
                did_well=["Should not leak into strengths"],
            ),
        ),
        _qa("q2", "rag", "answered", _record("correct", did_well=["Fine detail."])),
    ]

    report = ReportBuilder().build(session, qa_records)

    assert report.needs_review_count == 1
    assert "Should not leak into weaknesses" not in report.weaknesses
    assert "Should not leak into strengths" not in report.strengths
    assert "rag" not in report.topics_to_revisit  # needs_review doesn't feed topics either
    assert any(q.question_id == "q1" and q.needs_review for q in report.questions)


def test_answered_record_missing_eval_json_is_treated_as_needs_review():
    session = _session()
    qa_records = [_qa("q1", "rag", "answered", None)]

    report = ReportBuilder().build(session, qa_records)

    assert report.needs_review_count == 1
    assert report.questions[0].classification == "needs_review"


def test_dedup_is_case_insensitive_and_capped(monkeypatch=None):
    session = _session()
    qa_records = [
        _qa("q1", "rag", "answered", _record("correct", did_well=["Same point.", "same point."])),
        *[
            _qa(f"q{i}", "rag", "answered", _record("correct", did_well=[f"Unique point {i}"]))
            for i in range(2, 15)
        ],
    ]

    report = ReportBuilder().build(session, qa_records, max_items_per_section=5)

    assert report.strengths.count("Same point.") == 1  # dedup collapses the near-duplicate
    assert len(report.strengths) == 5  # capped


def test_render_markdown_includes_header_and_sections():
    session = _session()
    qa_records = [_qa("q1", "rag", "answered", _record("correct", did_well=["Good grasp of chunking."]))]
    report = ReportBuilder().build(session, qa_records)

    markdown = render_markdown(report)

    assert "# Viva Report" in markdown
    assert "owner/repo" in markdown
    assert "## Strengths" in markdown
    assert "Good grasp of chunking." in markdown
    assert "## Weaknesses" in markdown
    assert "_None noted._" in markdown  # weaknesses section is empty here
    assert "## Topics to Revisit" in markdown
    assert "## Question-by-Question" in markdown


def test_render_json_round_trips_report_shape():
    session = _session()
    qa_records = [_qa("q1", "rag", "answered", _record("partial", missed=[MissedPoint(point="X", cited_file="f.py")]))]
    report = ReportBuilder().build(session, qa_records)

    payload = json.loads(render_json(report))

    assert payload["session_id"] == "sess-1"
    assert payload["repo_slug"] == "owner/repo"
    assert payload["answered_count"] == 1
    assert payload["questions"][0]["question_id"] == "q1"
    assert payload["questions"][0]["classification"] == "partial"
