"""Structured LLM output contracts.

Phase 0 scope: only the classification-style call from the walking skeleton
(docs/plan.md Phase 0) is modeled here. The full 5-field Evaluation Record
(docs/design.md §6) — did_well / missed / did_wrong / improvement as a
separate free-text call — is Phase 7 work; deliberately not built yet so
Phase 0 stays a *thin* slice, per docs/system-design/02-iteration-log.md's
lesson about not front-loading component work before the risky assumptions
are validated.

Kept intentionally small-field per docs/system-design/01-resolved-decisions.md
§1.2: local-model JSON reliability degrades as schema field count grows, so
Phase 0's single call stays close to the "classification" half of the
eventual two-call split rather than trying to prove out the full Evaluation
Record schema in one shot.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Classification = Literal["correct", "partial", "incorrect", "not_attempted"]


class EvaluationResult(BaseModel):
    """Phase 0 structured evaluation output.

    `cited_file` is optional at the schema level but enforced at the
    application level (see llm_client.evaluate_answer): a `missed` or
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
