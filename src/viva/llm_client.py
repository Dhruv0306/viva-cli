"""LLM client interface and the Ollama implementation.

Per docs/design.md §10: `LLMClient` is a thin interface so nothing else in
the pipeline imports Ollama directly (NFR5). Phase 0 only implements the one
call the walking skeleton needs — `evaluate_answer` — using the 3-layer
structured-output reliability strategy from
docs/system-design/01-resolved-decisions.md §1.2:

  1. Grammar/schema-constrained decoding (Ollama's `format=<json schema>`).
  2. Pydantic validation on receipt.
  3. One repair re-prompt with the validation error attached; on a second
     failure, fall back to a `needs_review: true` record rather than
     blocking (never a hard failure).

The heavier "small-schema decomposition" and "async free-text call" pieces
of that strategy are Phase 7 scope once the full Evaluation Record exists.
"""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass

import ollama
from pydantic import ValidationError

from viva.schemas import EvaluationResult

EVALUATOR_SYSTEM_PROMPT = """You are grading a candidate's spoken answer in a \
code-grounded oral exam ("viva") about their own project.

Judge ONLY against the provided code context. If the code does not clearly \
demonstrate something, do not penalize the candidate for omitting it. Do not \
import outside best-practice opinions unless the code context contradicts \
the candidate's answer.

Classify the answer as exactly one of: correct, partial, incorrect, \
not_attempted. Use "not_attempted" for blank or "I don't know" answers -- \
never classify a skipped answer as "incorrect".

If you classify as "partial" or "incorrect", you MUST cite the specific \
file/function from the code context that grounds your verdict in \
`cited_file` (e.g. "src/payments/handler.py:42"). If you cannot point to a \
specific citation, classify as "correct" or "not_attempted" instead -- \
never produce an ungrounded criticism.

Respond with a `summary` of one or two sentences explaining the verdict."""


@dataclass(frozen=True)
class LLMCallResult:
    """Wraps a structured LLM result with the latency it took to produce.

    `duration_seconds` is what the caller (see viva.timer) excludes from the
    user-facing answer clock (docs/design.md §7, FR17).
    """

    result: EvaluationResult
    duration_seconds: float
    attempts: int


class LLMClient(abc.ABC):
    """Thin interface so pipeline code never imports a backend directly."""

    @abc.abstractmethod
    def evaluate_answer(
        self, question: str, ground_truth_context: str, user_answer: str
    ) -> LLMCallResult:
        """Produce a schema-validated EvaluationResult for one Q&A pair."""
        raise NotImplementedError


class OllamaClient(LLMClient):
    def __init__(
        self, model: str, temperature: float, host: str, timeout: float | None = 120.0
    ) -> None:
        self._model = model
        self._temperature = temperature
        # No timeout previously meant a single slow/stuck generation could
        # hang the whole process indefinitely with no error and no signal
        # to the caller (surfaced by the pressure-test harness hanging on
        # a larger candidate model). 120s is generous for a single
        # evaluate_answer call on commodity 7B-14B hardware; callers that
        # need something different (e.g. a slower box, a much bigger model)
        # can override it.
        self._client = ollama.Client(host=host, timeout=timeout)

    def evaluate_answer(
        self, question: str, ground_truth_context: str, user_answer: str
    ) -> LLMCallResult:
        prompt = self._build_prompt(question, ground_truth_context, user_answer)

        start = time.monotonic()
        attempts = 0
        last_error: str | None = None

        for attempt in (1, 2):
            attempts = attempt
            messages = [
                {"role": "system", "content": EVALUATOR_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
            if last_error is not None:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response failed schema validation "
                            f"with this error: {last_error}\n"
                            "Return ONLY valid JSON matching the schema."
                        ),
                    }
                )

            response = self._client.chat(
                model=self._model,
                messages=messages,
                format=EvaluationResult.model_json_schema(),
                options={"temperature": self._temperature},
            )
            raw = response["message"]["content"]

            try:
                parsed = EvaluationResult.model_validate_json(raw)
            except ValidationError as exc:
                last_error = str(exc)
                continue

            # Enforce FR22 at the application layer too, not just via the
            # system prompt: a partial/incorrect verdict with no citation is
            # ungrounded criticism and must be downgraded, not surfaced.
            if parsed.classification in ("partial", "incorrect") and not parsed.cited_file:
                parsed = parsed.model_copy(update={"needs_review": True})

            duration = time.monotonic() - start
            return LLMCallResult(result=parsed, duration_seconds=duration, attempts=attempts)

        # Both attempts failed schema validation: repair loop exhausted.
        # Never block the session on one bad parse (design.md §4/§9).
        fallback = EvaluationResult(
            classification="not_attempted",
            summary="Automated evaluation could not be produced reliably for this answer.",
            cited_file=None,
            needs_review=True,
        )
        duration = time.monotonic() - start
        return LLMCallResult(result=fallback, duration_seconds=duration, attempts=attempts)

    @staticmethod
    def _build_prompt(question: str, ground_truth_context: str, user_answer: str) -> str:
        # Explicitly labeled, non-concatenated sections per design.md §5 /
        # 01-resolved-decisions.md §1.3, so the model doesn't conflate "what
        # the code does" with "what the candidate said" or "best practice".
        return (
            f"[QUESTION]\n{question}\n\n"
            f"[GROUND_TRUTH_CODE_CONTEXT]\n{ground_truth_context}\n\n"
            f"[USER_ANSWER]\n{user_answer}\n"
        )
