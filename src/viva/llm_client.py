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
import logging
import time
from dataclasses import dataclass

import ollama
from pydantic import ValidationError

from viva.schemas import EvaluationResult

logger = logging.getLogger(__name__)

SUMMARIZE_FILE_SYSTEM_PROMPT = """You are summarizing one source file for a \
codebase-understanding tool. Ground the summary ONLY in the provided \
functions/classes/content -- do not guess at behavior that isn't shown.

Write a concise summary (roughly the requested length) of what this file \
does, in plain prose. No preamble, no markdown, no repeating the file path."""

REDUCE_SYSTEM_PROMPT = """You are combining several summaries into one \
higher-level summary for a codebase-understanding tool. Synthesize the \
common purpose and notable differences across them -- do not just \
concatenate or list them one by one.

Write a concise combined summary (roughly the requested length), in plain \
prose. No preamble, no markdown, no bullet list of the inputs."""

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

    @abc.abstractmethod
    def summarize_file(
        self, path: str, language: str | None, content_excerpt: str, target_tokens: int
    ) -> str:
        """Map step (FR7): produce a short free-text summary of one file.

        Free text, not a schema-validated structure -- the 3-layer
        reliability strategy (grammar-constrained decoding -> Pydantic
        validation -> repair loop) exists for the Evaluator's
        machine-consumed verdicts (docs/system-design/01-resolved-decisions.md
        §1.2); a prose summary has no such downstream parsing to protect.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def reduce(self, label: str, summaries: list[str], target_tokens: int) -> str:
        """Reduce step (FR7/FR8): combine several summaries into one.

        Reused at every reduce level -- per-module file-summary reduce,
        and (when the size check in
        docs/system-design/06-cli-contract-and-profile-scaling.md §6.2
        fails) each batch/recursion level of the architecture summary --
        since they're structurally identical, "combine N summaries into
        one at a target length," not three distinct operations.
        """
        raise NotImplementedError

    def get_context_window(self) -> int | None:
        """Best-effort lookup of `LLM_MODEL`'s context window, used to
        size a runtime default for `MAX_REDUCE_CONTEXT_TOKENS` when the
        user hasn't set one explicitly (§6.2: "should be computed as a
        fraction of the model's known context size... since LLM_MODEL is
        itself swappable"). Returns None if unavailable -- callers must
        fall back to a hardcoded conservative default in that case, this
        is deliberately not abstract/required so a test double doesn't
        need to implement it.
        """
        return None


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

    def summarize_file(
        self, path: str, language: str | None, content_excerpt: str, target_tokens: int
    ) -> str:
        prompt = (
            f"[FILE]\n{path}\n\n"
            f"[LANGUAGE]\n{language or 'unknown'}\n\n"
            f"[TARGET_LENGTH]\n~{target_tokens} tokens\n\n"
            f"[CONTENT]\n{content_excerpt}\n"
        )
        return self._generate(SUMMARIZE_FILE_SYSTEM_PROMPT, prompt, target_tokens)

    def reduce(self, label: str, summaries: list[str], target_tokens: int) -> str:
        joined = "\n\n".join(f"- {s}" for s in summaries)
        prompt = f"[{label}]\n\n[TARGET_LENGTH]\n~{target_tokens} tokens\n\n[SUMMARIES]\n{joined}\n"
        return self._generate(REDUCE_SYSTEM_PROMPT, prompt, target_tokens)

    def get_context_window(self) -> int | None:
        try:
            info = self._client.show(self._model)
        except Exception:  # noqa: BLE001 - best-effort only, see base class docstring
            return None

        # ollama-python's ShowResponse shape has shifted across versions;
        # `modelinfo`/`model_info` may be an attribute or a dict key, and
        # the context-length key is namespaced per model family (e.g.
        # "llama.context_length", "gemma3.context_length"). Search
        # defensively rather than assuming one exact shape.
        model_info = getattr(info, "modelinfo", None)
        if model_info is None and isinstance(info, dict):
            model_info = info.get("model_info") or info.get("modelinfo")
        if not model_info:
            return None

        items = model_info.items() if hasattr(model_info, "items") else []
        for key, value in items:
            if isinstance(key, str) and key.endswith("context_length"):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return None

    def _generate(self, system_prompt: str, user_prompt: str, target_tokens: int) -> str:
        response = self._client.chat(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            # Disable extended thinking explicitly for reasoning-capable
            # models. Discovered against a real Ollama run: summarize_file/
            # reduce cap num_predict (evaluate_answer doesn't -- see below),
            # and a thinking model spends that budget on hidden <think>
            # reasoning before ever emitting visible content, coming back
            # empty. evaluate_answer never hit this because it sets no
            # num_predict cap at all, giving a thinking model room to
            # finish reasoning before the budget runs out -- but doing
            # that here too would make the Map step (one call per sampled
            # file, potentially hundreds per repo) unpredictably slow for
            # no summarization-quality benefit. think=False is a no-op for
            # non-reasoning models, so this is safe either way.
            think=False,
            options={
                "temperature": self._temperature,
                # 3x the target with a 128-token floor -- generous enough
                # that even a model which doesn't fully respect think=False
                # (or pads before settling into the answer) still has room
                # to produce real content instead of getting cut off.
                "num_predict": max(int(target_tokens * 3), 128),
            },
        )
        content = response["message"]["content"].strip()
        if not content:
            # Never let a blank LLM response propagate silently into a
            # blank Project Profile field -- that's exactly what produced
            # confusing cascading output before this fix (a reduce() call
            # over several empty summaries got a "please provide the
            # summaries" response from the model, since there was nothing
            # in them to synthesize).
            logger.warning(
                "Empty content from model '%s' for a summarize/reduce call "
                "(target_tokens=%d) despite think=False and num_predict=%d -- "
                "the model may not respect think=False, or generation was "
                "cut off before any visible content.",
                self._model, target_tokens, max(int(target_tokens * 3), 128),
            )
            return "(summary unavailable: the LLM returned no content for this call)"
        return content

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
