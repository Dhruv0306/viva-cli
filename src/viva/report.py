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

import re

import html
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, UTC

from viva.citations import normalize_citation
from viva.schemas import EvaluationRecord
from viva.storage.session_store import (
    ANSWERED,
    ASKED,
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
# order is the order known-reason notes are rendered in; any status not
# in this map (e.g. a future addition, or ASKED surfacing here would be
# a bug since it's listed explicitly below) still gets a note via the
# _UNKNOWN_REASON_TEXT fallback in build() rather than being dropped.
_UNANSWERED_REASON_TEXT = {
    PENDING: "planned but not reached before the session ended",
    ASKED: "asked but not yet answered (partial report)",
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


# A model sometimes returns "None." (or similar) as a strength or weakness
# instead of an empty list. docs/system-design/28-phase-23-validation-
# followups-design.md §28.2. Precise on purpose: a real finding can start
# with "None of the error paths are tested", which must be kept, so "none"
# alone only counts when followed by punctuation or the end of the text.
_NON_FINDING = re.compile(
    r"^(?:none|n/a|nil|nothing)\s*(?:[.:;!,(]|$)"
    r"|^(?:none|nothing)\s+(?:noted|identified|found|observed|significant|to\s+report)\b"
    r"|^no\s+(?:(?:significant|major|notable)\s+)?"
    r"(?:weaknesses|weakness|issues|gaps|problems)\b",
    re.IGNORECASE,
)


def _is_non_finding(text: str) -> bool:
    return _NON_FINDING.match(text.strip()) is not None


def _format_point(point: str, cited_file: str | None) -> str:
    # Phase 26 (docs/system-design/31-phase-26-followups-design.md §31.5):
    # sessions stored before citation validation existed can hold "N/A"-
    # style text in cited_file; show those points without a citation
    # rather than as "(N/A (Not present in the policy file))".
    citation = normalize_citation(cited_file)
    return f"{point} ({citation})" if citation else point


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

        # §13.4: unanswered qa_records (pending/skipped_*/asked/anything
        # else) are never silently dropped -- surfaced as coverage notes
        # instead of folded into strengths/weaknesses. The first version
        # of this fix only iterated over known statuses in
        # _UNANSWERED_REASON_TEXT, which reproduced the exact same
        # silently-dropped-question bug for any status outside that map
        # (notably ASKED, which is reachable via `viva report
        # --allow-partial` on an in-progress session) -- caught in code
        # review, see test_coverage_notes_never_drop_an_unrecognized_status.
        unanswered_counts: dict[str, int] = {}
        for r in qa_records:
            if r.status != ANSWERED:
                unanswered_counts[r.status] = unanswered_counts.get(r.status, 0) + 1
        # Known statuses render in _UNANSWERED_REASON_TEXT's order; any
        # status not in that map still gets a note (sorted for
        # deterministic output) rather than vanishing.
        ordered_statuses = [s for s in _UNANSWERED_REASON_TEXT if s in unanswered_counts]
        ordered_statuses += sorted(s for s in unanswered_counts if s not in _UNANSWERED_REASON_TEXT)
        coverage_notes = [
            f"{unanswered_counts[status]} question{'s' if unanswered_counts[status] != 1 else ''} "
            f"{_UNANSWERED_REASON_TEXT.get(status, f'in status {status!r}')}."
            for status in ordered_statuses
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
                strength_candidates.extend(
                    t for t in evaluation.did_well if not _is_non_finding(t)
                )

            if classification_label in _WEAKNESS_CLASSIFICATIONS:
                weakness_candidates.extend(
                    _format_point(m.point, m.cited_file)
                    for m in evaluation.missed
                    if not _is_non_finding(m.point)
                )
                weakness_candidates.extend(
                    _format_point(m.point, m.cited_file)
                    for m in evaluation.did_wrong
                    if not _is_non_finding(m.point)
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
            generated_at=datetime.now(UTC).isoformat(),
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
    downstream tooling. `Report`'s dataclass shape is the schema (§13.5)
    -- `asdict()` recursively converts `Report` (and its nested
    `QuestionSummary` entries) into a plain dict field-by-field, in
    declaration order, with no separate schema to hand-maintain."""
    return json.dumps(asdict(report), indent=2)


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _render_html_section(heading: str, items: list[str]) -> str:
    if not items:
        body = '<p class="report-empty">None noted.</p>'
    else:
        body = "<ul>" + "".join(f"<li>{_esc(item)}</li>" for item in items) + "</ul>"
    return f"<section><h2>{_esc(heading)}</h2>{body}</section>"


def render_html(report: Report) -> str:
    """Renders `report` as a self-contained HTML fragment (no
    `<html>`/`<head>`/`<body>` -- meant to be embedded in an existing
    page, e.g. viva-web's report view, docs/system-design/
    15-phase-10-web-ui-design.md §15.15) for display, and as a `<table>`/
    `<dl>`/`<ul>` structure instead of the Markdown table `render_markdown`
    produces.

    Built directly from the same structured `Report` object
    `render_markdown`/`render_json` already use, not by parsing
    `render_markdown`'s output -- there's no Markdown-parsing step to get
    wrong or keep in sync as `Report`'s shape evolves.

    Every string here can originate from LLM output grounded in the
    analyzed repo's own content (a question's category/classification/
    summary, a strengths/weaknesses point, coverage notes derived from
    persisted question text) -- `html.escape()` is applied to all of it
    before embedding, since a repo crafted to prompt-inject HTML/script
    content into an LLM's answer should not be able to run script in
    whoever's browser is viewing the report."""
    title = _esc(report.repo_slug or report.session_id)
    parts: list[str] = [f"<article><h1>Viva Report — {title}</h1>"]

    # Wrapped in <aside>/<div> (rather than left as flat <article>
    # children) so the web UI's CSS can lay this out as an independent-
    # height sidebar next to the report body -- plain CSS Grid
    # auto-placement on a flat sibling list locks every row's height to
    # the tallest item across *both* columns, which stretches a short
    # metadata block to match whatever the longest strengths/weaknesses
    # section happens to be. Two real containers side-step that
    # entirely; this costs nothing for `render_markdown`/`render_json`,
    # which don't go through this function at all.
    parts.append('<aside class="report-meta-rail">')
    parts.append('<dl class="report-meta">')
    parts.append(f"<dt>Session</dt><dd>{_esc(report.session_id)}</dd>")
    parts.append(f"<dt>Commit</dt><dd>{_esc(report.commit_sha or '(unknown)')}</dd>")
    parts.append(f"<dt>Status</dt><dd>{_esc(report.status)}</dd>")
    parts.append(f"<dt>Generated</dt><dd>{_esc(report.generated_at)}</dd>")
    parts.append(
        f"<dt>Answered</dt><dd>{report.answered_count}/{report.total_questions} "
        f"(needs review: {report.needs_review_count})</dd>"
    )
    parts.append("</dl>")

    if report.coverage_notes:
        parts.append('<ul class="report-coverage-notes">')
        parts.extend(f"<li>{_esc(note)}</li>" for note in report.coverage_notes)
        parts.append("</ul>")
    parts.append("</aside>")

    parts.append('<div class="report-content">')
    parts.append(_render_html_section("Strengths", report.strengths))
    parts.append(_render_html_section("Weaknesses", report.weaknesses))
    parts.append(_render_html_section("Topics to Revisit", report.topics_to_revisit))

    parts.append("<section><h2>Question-by-Question</h2>")
    parts.append(
        '<table class="report-questions"><thead><tr>'
        "<th>Category</th><th>Classification</th><th>Summary</th>"
        "</tr></thead><tbody>"
    )
    for q in report.questions:
        classification = f"{q.classification} (needs review)" if q.needs_review else q.classification
        row_class = ' class="needs-review"' if q.needs_review else ""
        parts.append(
            f"<tr{row_class}><td>{_esc(q.category)}</td>"
            f"<td>{_esc(classification)}</td>"
            f"<td>{_esc(q.summary)}</td></tr>"
        )
    parts.append("</tbody></table></section>")

    parts.append("</div></article>")
    return "".join(parts)
