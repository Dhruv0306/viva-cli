"""Structured LLM output contracts.

Phase 0 modeled only the classification-style call from the walking
skeleton (docs/plan.md Phase 0): `ClassificationResult` (originally named
`EvaluationResult`, renamed here since it's now explicitly call #1 of two).
Phase 7 (docs/system-design/12-phase-7-evaluator-design.md) adds the
second call's schema, `EvaluationFeedback`, and `EvaluationRecord`, the
combined shape persisted to `qa_records.eval_json` (docs/design.md §6).

Kept intentionally small-field per docs/system-design/01-resolved-decisions.md
§1.2: local-model JSON reliability degrades as schema field count grows, so
each call stays close to one concern -- `ClassificationResult` is the
fast verdict, `EvaluationFeedback` is the free-text detail conditioned on
that verdict -- rather than one large schema for both.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Classification = Literal["correct", "partial", "incorrect", "not_attempted"]


class ClassificationResult(BaseModel):
    """Call #1: the fast verdict (docs/system-design/
    12-phase-7-evaluator-design.md §12.2). Drives FR14's follow-up
    decision directly, synchronously, before any feedback text exists.

    `cited_file` is optional at the schema level but enforced at the
    application level (see llm_client.classify_answer): a `partial` or
    `incorrect` classification with no citation is downgraded to
    `needs_review` rather than shown to the user ungrounded
    (docs/design.md §5 / FR22).
    """

    classification: Classification
    summary: str = Field(
        ..., min_length=1, max_length=500, description="One or two sentence verdict."
    )
    cited_file: Optional[str] = Field(
        default=None,
        description=(
            "Specific file/function the verdict is grounded in, e.g. "
            "'src/payments/handler.py:42'. Required to support a "
            "'partial' or 'incorrect' classification (FR22)."
        ),
    )
    needs_review: bool = Field(
        default=False,
        description="Set by the client (not the model) when structured output "
        "could not be reliably obtained after the repair loop (design.md §4).",
    )


class MissedPoint(BaseModel):
    """One entry in `EvaluationFeedback.missed`/`did_wrong`. `cited_file`
    is optional at the schema level for the same reason as
    `ClassificationResult.cited_file` -- FR22 enforcement (dropping
    uncited entries) happens at the application layer in
    `viva.evaluator`, not here."""

    point: str = Field(..., min_length=1, max_length=300)
    cited_file: Optional[str] = None


class EvaluationFeedback(BaseModel):
    """Call #2: the free-text detail, generated with the call #1 verdict
    already in context (docs/system-design/12-phase-7-evaluator-design.md
    §12.2) so the prompt doesn't ask for `did_wrong` padding on a
    `correct` answer or `did_well` padding on an `incorrect` one.

    `needs_review` mirrors `ClassificationResult.needs_review`'s
    client-set-not-model-set convention: forced `True` by
    `viva.evaluator` if FR22 filtering empties both `missed` and
    `did_wrong` while the verdict was `partial`/`incorrect` (an
    unsubstantiated critical verdict is worse than an admittedly
    incomplete one), or if the repair loop is exhausted.
    """

    did_well: list[str] = Field(default_factory=list)
    missed: list[MissedPoint] = Field(default_factory=list)
    did_wrong: list[MissedPoint] = Field(default_factory=list)
    improvement: str = Field(
        ..., min_length=1, max_length=500, description="One or two sentence, forward-looking suggestion."
    )
    needs_review: bool = Field(default=False)


class EvaluationRecord(BaseModel):
    """The combined shape persisted to `qa_records.eval_json`
    (docs/design.md §6, docs/system-design/12-phase-7-evaluator-design.md
    §12.2) -- call #1's verdict plus call #2's detail, merged once both
    have run."""

    classification: Classification
    summary: str
    cited_file: Optional[str] = None
    did_well: list[str] = Field(default_factory=list)
    missed: list[MissedPoint] = Field(default_factory=list)
    did_wrong: list[MissedPoint] = Field(default_factory=list)
    improvement: str
    needs_review: bool = False

    @classmethod
    def from_calls(
        cls, classification: ClassificationResult, feedback: EvaluationFeedback
    ) -> "EvaluationRecord":
        return cls(
            classification=classification.classification,
            summary=classification.summary,
            cited_file=classification.cited_file,
            did_well=feedback.did_well,
            missed=feedback.missed,
            did_wrong=feedback.did_wrong,
            improvement=feedback.improvement,
            needs_review=classification.needs_review or feedback.needs_review,
        )
