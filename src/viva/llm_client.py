"""LLM client interface and the Ollama implementation.

Per docs/design.md §10: `LLMClient` is a thin interface so nothing else in
the pipeline imports Ollama directly (NFR5). Phase 0 implemented the one
call the walking skeleton needed -- originally named `evaluate_answer` --
using the 3-layer structured-output reliability strategy from
docs/system-design/01-resolved-decisions.md §1.2:

  1. Grammar/schema-constrained decoding (Ollama's `format=<json schema>`).
  2. Pydantic validation on receipt.
  3. One repair re-prompt with the validation error attached; on a second
     failure, fall back to a `needs_review: true` record rather than
     blocking (never a hard failure).

Phase 7 (docs/system-design/12-phase-7-evaluator-design.md §12.2) splits
that one call into two, both following the same 3-layer strategy:
`classify_answer` (the fast verdict, `evaluate_answer` renamed) and
`generate_feedback` (the free-text detail, conditioned on the verdict).
"""
from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass
from typing import Union

import ollama
from pydantic import ValidationError

from viva.schemas import ClassificationResult, EvaluationFeedback

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

QUESTION_GEN_SYSTEM_PROMPT = """You are writing one oral-exam ("viva") \
question for a candidate about their own codebase.

Ground the question ONLY in the provided code context -- ask about \
something the context actually shows, never a generic question that \
could be answered without having read this specific code (e.g. never \
"describe your architecture" in isolation; instead ask about the \
specific pattern/decision/module the context demonstrates).

Keep it to ONE clause, ONE sentence, roughly 15-25 words -- a real \
examiner asks one thing at a time, not a chain of conditions. Do NOT \
stack qualifiers with "if X and Y", "especially when Z", or "given \
that ...". Being specific means naming the exact function/class/\
parameter the context shows, not piling on every edge case it handles.

Good: "Why does `_resolve_context` set `resilient_parsing=True` on the \
child context but not the parent?"
Bad (too many clauses): "When `_resolve_context` traverses a `Group` \
with `chain` enabled, how does it ensure the context hierarchy stays \
accurate, especially when `resilient_parsing` is set and a subcommand \
also defines its own chain?"

Ask exactly ONE question. Write it as a direct, spoken-style question a \
human examiner would ask out loud. No preamble, no markdown, no \
numbering, no restating the code context back verbatim.

If an [AVOID_REPEATING] section lists questions already asked this \
session, do not ask something that tests substantially the same \
understanding, even worded differently (e.g. don't ask "why does X not \
need parameter Y" twice about the same code just because the phrasing \
differs) -- pick a different angle, method, parameter, or code path \
from the context instead."""

CLASSIFICATION_SYSTEM_PROMPT = """You are grading a candidate's spoken answer in a \
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

FEEDBACK_SYSTEM_PROMPT = """You are writing detailed feedback on a candidate's \
spoken answer in a code-grounded oral exam ("viva") about their own project. \
A verdict (`classification`, `summary`) has already been produced for this \
answer -- your job is to explain it in more depth, not to re-grade it.

Judge ONLY against the provided code context. Every entry in `missed` or \
`did_wrong` MUST cite the specific file/function that grounds it in \
`cited_file` (e.g. "src/payments/handler.py:42") -- never produce an \
ungrounded criticism. If you cannot point to a specific citation for a \
point, leave it out rather than including it uncited.

Tailor the balance of fields to the verdict already given: a "correct" \
answer should have a full `did_well` and little or nothing in `missed`/\
`did_wrong`; an "incorrect" answer should have little or nothing in \
`did_well`. Do not pad a list just to fill it.

`improvement` is one or two sentences of forward-looking, actionable \
advice -- not a repeat of `missed`/`did_wrong`, but what to go read or \
think about next."""


@dataclass(frozen=True)
class LLMCallResult:
    """Wraps a structured LLM result with the latency it took to produce.

    `result` is `ClassificationResult` for `classify_answer` calls or
    `EvaluationFeedback` for `generate_feedback` calls -- one wrapper
    shape for both, since callers (viva.evaluator) already know which
    call they made and don't need this to discriminate.

    `duration_seconds` is what the caller (see viva.timer) excludes from the
    user-facing answer clock (docs/design.md §7, FR17).
    """

    result: Union[ClassificationResult, EvaluationFeedback]
    duration_seconds: float
    attempts: int


class LLMClient(abc.ABC):
    """Thin interface so pipeline code never imports a backend directly."""

    @abc.abstractmethod
    def classify_answer(
        self, question: str, ground_truth_context: str, user_answer: str
    ) -> LLMCallResult:
        """Call #1 (docs/system-design/12-phase-7-evaluator-design.md
        §12.2): produce a schema-validated `ClassificationResult` for one
        Q&A pair. Fast, drives FR14's follow-up decision synchronously."""
        raise NotImplementedError

    @abc.abstractmethod
    def generate_feedback(
        self,
        question: str,
        ground_truth_context: str,
        user_answer: str,
        classification: ClassificationResult,
    ) -> LLMCallResult:
        """Call #2: produce a schema-validated `EvaluationFeedback` for
        one Q&A pair, conditioned on call #1's already-produced verdict.
        Slower; the Evaluator backgrounds this (docs/system-design/
        12-phase-7-evaluator-design.md §12.4)."""
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
    def generate_question(
        self, category: str, target_module: str | None, grounding_context: str,
        target_file: str | None = None, avoid_questions: list[str] | None = None,
    ) -> str:
        """FR13: just-in-time question generation, grounded in retrieved
        chunk text (`grounding_context`).

        Free text, not a schema-validated structure -- same rationale as
        `summarize_file`/`reduce`: nothing downstream parses this as
        structured data. Grounding is guaranteed by the caller
        (`questiongen/retrieval.py`) supplying real retrieved chunks as
        `grounding_context`, not by the model self-reporting what it used.

        `target_file`, when set (Pass 3's file-level plan items --
        `questiongen/planner.py`), narrows the question to that specific
        file rather than the module broadly.

        `avoid_questions`, when set (Phase 6, docs/system-design/
        11-phase-6-session-loop-design.md §11.12), lists question texts
        already asked this session -- the model is asked not to generate
        something that tests substantially the same understanding, even
        if worded differently. This is the primary FR15 defense (giving
        the model in-context awareness so it doesn't produce a near-
        duplicate in the first place); the Orchestrator's embedding-
        similarity check is a backstop for when the model doesn't fully
        comply, not a replacement for this.
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
        # classify_answer/generate_feedback call on commodity 7B-14B
        # hardware; callers that need something different (e.g. a slower
        # box, a much bigger model) can override it.
        self._client = ollama.Client(host=host, timeout=timeout)

    def classify_answer(
        self, question: str, ground_truth_context: str, user_answer: str
    ) -> LLMCallResult:
        prompt = self._build_prompt(question, ground_truth_context, user_answer)

        start = time.monotonic()
        attempts = 0
        last_error: str | None = None

        for attempt in (1, 2):
            attempts = attempt
            messages = [
                {"role": "system", "content": CLASSIFICATION_SYSTEM_PROMPT},
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
                format=ClassificationResult.model_json_schema(),
                options={"temperature": self._temperature},
            )
            raw = response["message"]["content"]

            try:
                parsed = ClassificationResult.model_validate_json(raw)
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
        fallback = ClassificationResult(
            classification="not_attempted",
            summary="Automated evaluation could not be produced reliably for this answer.",
            cited_file=None,
            needs_review=True,
        )
        duration = time.monotonic() - start
        return LLMCallResult(result=fallback, duration_seconds=duration, attempts=attempts)

    def generate_feedback(
        self,
        question: str,
        ground_truth_context: str,
        user_answer: str,
        classification: ClassificationResult,
    ) -> LLMCallResult:
        prompt = self._build_prompt(question, ground_truth_context, user_answer) + (
            f"\n[VERDICT_ALREADY_GIVEN]\nclassification: {classification.classification}\n"
            f"summary: {classification.summary}\n"
        )

        start = time.monotonic()
        attempts = 0
        last_error: str | None = None

        for attempt in (1, 2):
            attempts = attempt
            messages = [
                {"role": "system", "content": FEEDBACK_SYSTEM_PROMPT},
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
                format=EvaluationFeedback.model_json_schema(),
                options={"temperature": self._temperature},
            )
            raw = response["message"]["content"]

            try:
                parsed = EvaluationFeedback.model_validate_json(raw)
            except ValidationError as exc:
                last_error = str(exc)
                continue

            # FR22 at the application layer: drop any missed/did_wrong
            # entry with no citation rather than surface it ungrounded.
            grounded_missed = [p for p in parsed.missed if p.cited_file]
            grounded_did_wrong = [p for p in parsed.did_wrong if p.cited_file]
            dropped_any = (
                len(grounded_missed) < len(parsed.missed)
                or len(grounded_did_wrong) < len(parsed.did_wrong)
            )
            needs_review = parsed.needs_review
            if dropped_any and classification.classification in ("partial", "incorrect") \
                    and not grounded_missed and not grounded_did_wrong:
                # Dropping uncited entries emptied both lists on a
                # critical verdict -- an unsubstantiated criticism is
                # worse than an admittedly incomplete one.
                needs_review = True
            parsed = parsed.model_copy(
                update={
                    "missed": grounded_missed,
                    "did_wrong": grounded_did_wrong,
                    "needs_review": needs_review,
                }
            )

            duration = time.monotonic() - start
            return LLMCallResult(result=parsed, duration_seconds=duration, attempts=attempts)

        # Both attempts failed schema validation: repair loop exhausted.
        # Never block the session on one bad parse (design.md §4/§9).
        fallback = EvaluationFeedback(
            did_well=[],
            missed=[],
            did_wrong=[],
            improvement="Automated feedback could not be produced reliably for this answer.",
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

    def generate_question(
        self, category: str, target_module: str | None, grounding_context: str,
        target_file: str | None = None, avoid_questions: list[str] | None = None,
    ) -> str:
        # Explicitly labeled, non-concatenated sections, same convention
        # as _build_prompt's evaluator prompt (design.md §5).
        sections = [
            f"[CATEGORY]\n{category}",
            f"[TARGET_MODULE]\n{target_module or '(project-level)'}",
        ]
        if target_file:
            sections.append(f"[TARGET_FILE]\n{target_file}")
        if avoid_questions:
            avoid_list = "\n".join(f"- {q}" for q in avoid_questions)
            sections.append(f"[AVOID_REPEATING]\n{avoid_list}")
        sections.append(f"[CODE_CONTEXT]\n{grounding_context}")
        prompt = "\n\n".join(sections) + "\n"
        # A single spoken question is short -- a smaller fixed target
        # than summarize_file/reduce's variable target_tokens is enough
        # headroom, while still giving a thinking-capable model (think=False
        # notwithstanding, see _generate's comment) room to not get cut off.
        return self._generate(QUESTION_GEN_SYSTEM_PROMPT, prompt, target_tokens=80)

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
            # reduce cap num_predict (classify_answer/generate_feedback
            # don't -- see below), and a thinking model spends that budget
            # on hidden <think> reasoning before ever emitting visible
            # content, coming back empty. classify_answer/generate_feedback
            # never hit this because they set no num_predict cap at all,
            # giving a thinking model room to
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
