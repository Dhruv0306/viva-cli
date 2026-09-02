"""Report aggregation and rendering (FR25-FR27, docs/plan.md Phase 8).

`ReportBuilder` is a pure, lazy reader over already-persisted state
(`SessionRecord` + `QARecordRow`s from `SessionStore`) -- it has no I/O of
its own and is invoked on demand by the `viva report` CLI command, not by
the Orchestrator during a live session. See
docs/system-design/13-phase-8-report-design.md §13.2 for the rationale
(no staleness story to maintain, no new column/state needed).

`Report`'s own dataclass shape *is* the JSON schema for
`viva report --format json` (§13.5) -- there's no separate hand-maintained
schema to keep in sync with `EvaluationRecord`, since `Report` is already
a purpose-built aggregate rather than a pass-through of it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from viva.schemas import EvaluationRecord
from viva.storage.session_store import (
    ANSWERED,
    PENDING,
    QARecordRow,
    SKIPPED_DUPLICATE_TARGET,
    SKIPPED_NO_GROUNDING,
    SKIPPED_TIME_COLLAPSE,
    SessionRecord,
)

# Classifications whose did_well text is eligible for the strengths rollup.
_STRENGTH_CLASSIFICATIONS = {"correct", "partial"}
# Classifications whose missed/did_wrong text is eligible for the
# weaknesses rollup, and whose category feeds topics-to-revisit.
_WEAKNESS_CLASSIFICATIONS = {"partial", "incorrect"}

# Human-readable reasons for qa_records that never got an answer (§13.4:
# these are reported as a coverage note, never silently dropped). Dict
# order is the order notes are rendered in.
_UNANSWERED_REASON_TEXT = {
    PENDING: "planned but not reached before the session ended",
    SKIPPED_TIME_COLLAPSE: "skipped (time budget collapse)",
    SKIPPED_DUPLICATE_TARGET: "skipped (duplicate target module)",
    SKIPPED_NO_GROUNDING: "skipped (no grounding found)",
}


@dataclass(frozen=True)
class ReportSection:
    """One rolled-up section (strengths/weaknesses) as an ordered,
    deduplicated, capped list. An internal aggregation building block --
    `Report` stores the resulting `items` lists directly (§13.4)."""

    heading: str
    items: list[str]


@dataclass(frozen=True)
class QuestionSummary:
    """One row of the per-question table (§13.4/13.5). Deliberately
    excludes full did_well/missed/did_wrong detail -- that's what
    `--format json` is for; the Markdown table stays scannable."""

    question_id: str
    category: str
    question_text: str | None
    classification: str  # Classification | "needs_review"
    summary: str
    needs_review: bool


@dataclass(frozen=True)
class Report:
    session_id: str
    repo_slug: str | None
    commit_sha: str | None
    status: str
    generated_at: str
    total_questions: int
    answered_count: int
    classification_counts: dict[str, int]
    strengths: list[str]
    weaknesses: list[str]
    topics_to_revisit: list[str]
    needs_review_count: int
    coverage_notes: list[str] = field(default_factory=list)
    questions: list[QuestionSummary] = field(default_factory=list)


def _parse_eval(eval_json: str | None) -> EvaluationRecord | None:
    if not eval_json:
        return None
    return EvaluationRecord.model_validate_json(eval_json)


def _dedup_capped(items: list[str], max_items: int) -> list[str]:
    """Case-insensitive dedup, preserving first-seen order, then capped
    (§13.4) -- mirrors qa_records' implicit rowid ordering used elsewhere
    in the project for stable, deterministic output."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item.strip())
    return result[:max_items]


def _format_point(point: str, cited_file: str | None) -> str:
    return f"{point} ({cited_file})" if cited_file else point


class ReportBuilder:
    """Aggregates a session's `qa_records` into a `Report`
    (docs/system-design/13-phase-8-report-design.md §13.4)."""

    def build(
        self,
        session: SessionRecord,
        qa_records: list[QARecordRow],
        max_items_per_section: int = 10,
    ) -> Report:
        answered = [r for r in qa_records if r.status == ANSWERED]

        # §13.4: unanswered qa_records (pending/skipped_*) are never
        # silently dropped -- they're surfaced as coverage notes instead
        # of folded into strengths/weaknesses. Root-caused against a real
        # `--duration 8` session where a planned question was never
        # reached and simply vanished from the report; see
        # test_coverage_notes_surface_unanswered_records_by_reason.
        unanswered_counts: dict[str, int] = {}
        for r in qa_records:
            if r.status != ANSWERED:
                unanswered_counts[r.status] = unanswered_counts.get(r.status, 0) + 1
        coverage_notes = [
            f"{count} question{'s' if count != 1 else ''} {_UNANSWERED_REASON_TEXT.get(status, status)}."
            for status, count in (
                (status, unanswered_counts[status])
                for status in _UNANSWERED_REASON_TEXT
                if status in unanswered_counts
            )
        ]

        classification_counts: dict[str, int] = {}
        strength_candidates: list[str] = []
        weakness_candidates: list[str] = []
        topic_order: list[str] = []
        topic_counts: dict[str, int] = {}
        needs_review_count = 0
        questions: list[QuestionSummary] = []

        for record in answered:
            evaluation = _parse_eval(record.eval_json)
            if evaluation is None:
                # answered but never evaluated (shouldn't happen once
                # SUMMARIZING's integrity check lands, §13.3, but this is
                # a report-time read -- never trust upstream invariants
                # blindly when producing user-facing output).
                classification_label = "needs_review"
                needs_review_count += 1
                questions.append(
                    QuestionSummary(
                        question_id=record.question_id,
                        category=record.category,
                        question_text=record.question_text,
                        classification=classification_label,
                        summary="No evaluation recorded.",
                        needs_review=True,
                    )
                )
                continue

            classification_label = evaluation.classification
            classification_counts[classification_label] = (
                classification_counts.get(classification_label, 0) + 1
            )
            if evaluation.needs_review:
                needs_review_count += 1

            questions.append(
                QuestionSummary(
                    question_id=record.question_id,
                    category=record.category,
                    question_text=record.question_text,
                    classification=classification_label,
                    summary=evaluation.summary,
                    needs_review=evaluation.needs_review,
                )
            )

            # needs_review records are surfaced individually above, but
            # excluded from rollups -- an unsubstantiated verdict
            # shouldn't be laundered into an aggregate statement (§13.4).
            if evaluation.needs_review:
                continue

            if classification_label in _STRENGTH_CLASSIFICATIONS:
                strength_candidates.extend(evaluation.did_well)

            if classification_label in _WEAKNESS_CLASSIFICATIONS:
                weakness_candidates.extend(
                    _format_point(m.point, m.cited_file) for m in evaluation.missed
                )
                weakness_candidates.extend(
                    _format_point(m.point, m.cited_file) for m in evaluation.did_wrong
                )

            if classification_label in _WEAKNESS_CLASSIFICATIONS:
                if record.category not in topic_counts:
                    topic_order.append(record.category)
                topic_counts[record.category] = topic_counts.get(record.category, 0) + 1

        topics_to_revisit = sorted(
            topic_order, key=lambda category: -topic_counts[category]
        )

        return Report(
            session_id=session.session_id,
            repo_slug=session.repo_slug,
            commit_sha=session.commit_sha,
            status=session.status,
            generated_at=datetime.now(timezone.utc).isoformat(),
            total_questions=len(qa_records),
            answered_count=len(answered),
            classification_counts=classification_counts,
            strengths=_dedup_capped(strength_candidates, max_items_per_section),
            weaknesses=_dedup_capped(weakness_candidates, max_items_per_section),
            topics_to_revisit=topics_to_revisit,
            needs_review_count=needs_review_count,
            coverage_notes=coverage_notes,
            questions=questions,
        )


def _render_section(heading: str, items: list[str]) -> str:
    lines = [f"## {heading}", ""]
    if not items:
        lines.append("_None noted._")
    else:
        lines.extend(f"- {item}" for item in items)
    lines.append("")
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    """Renders `report` as the FR26 default Markdown format
    (§13.5): header, coverage summary, strengths/weaknesses/
    topics-to-revisit sections, then a per-question table."""
    lines: list[str] = []
    lines.append(f"# Viva Report — {report.repo_slug or report.session_id}")
    lines.append("")
    lines.append(f"- **Session:** {report.session_id}")
    lines.append(f"- **Commit:** {report.commit_sha or '(unknown)'}")
    lines.append(f"- **Status:** {report.status}")
    lines.append(f"- **Generated:** {report.generated_at}")
    lines.append(
        f"- **Answered:** {report.answered_count}/{report.total_questions} "
        f"(needs review: {report.needs_review_count})"
    )
    for note in report.coverage_notes:
        lines.append(f"  - {note}")
    lines.append("")

    lines.append(_render_section("Strengths", report.strengths))
    lines.append(_render_section("Weaknesses", report.weaknesses))
    lines.append(_render_section("Topics to Revisit", report.topics_to_revisit))

    lines.append("## Question-by-Question")
    lines.append("")
    lines.append("| Category | Classification | Summary |")
    lines.append("|---|---|---|")
    for q in report.questions:
        classification = f"{q.classification} (needs review)" if q.needs_review else q.classification
        summary = q.summary.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {q.category} | {classification} | {summary} |")
    lines.append("")

    return "\n".join(lines)


def render_json(report: Report) -> str:
    """Renders `report` as FR26's `--format json` alternative for
    downstream tooling. `Report`'s dataclass shape is the schema (§13.5)."""
    payload = {
        "session_id": report.session_id,
        "repo_slug": report.repo_slug,
        "commit_sha": report.commit_sha,
        "status": report.status,
        "generated_at": report.generated_at,
        "total_questions": report.total_questions,
        "answered_count": report.answered_count,
        "classification_counts": report.classification_counts,
        "strengths": report.strengths,
        "weaknesses": report.weaknesses,
        "topics_to_revisit": report.topics_to_revisit,
        "needs_review_count": report.needs_review_count,
        "coverage_notes": report.coverage_notes,
        "questions": [
            {
                "question_id": q.question_id,
                "category": q.category,
                "question_text": q.question_text,
                "classification": q.classification,
                "summary": q.summary,
                "needs_review": q.needs_review,
            }
            for q in report.questions
        ],
    }
    return json.dumps(payload, indent=2)
