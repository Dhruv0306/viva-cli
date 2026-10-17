"""Recognize an answer that attempts nothing (docs/plan.md Phase 27,
docs/system-design/32-phase-27-duplicate-questions-design.md §32.5).

The model graded the answer `x` as `incorrect` for some questions and
`not_attempted` for others in the same session, and only `incorrect` queues
a follow-up. Recognizing a non-answer in code makes the grade the same every
time, skips two model calls, and queues no follow-up.

Conservative on purpose: a wrongly rejected answer is worse than a wrongly
accepted one, because the second only costs a model call. Two-letter words
("no", "ok", "id") are real answers, so only a single letter is judged
short, and only when that letter does not appear as a whole word in the code
the question is about: `i` is a correct answer to "what is the loop
variable" and appears in that code, `x` is not and does not.
"""
from __future__ import annotations

import re

NON_ANSWER_SUMMARY = "No substantive answer was given."
NON_ANSWER_IMPROVEMENT = (
    "Answer in your own words, even if you are unsure; a partial attempt shows what "
    "you understand about this code."
)

# Phrases that say "I am not attempting this". Kept to the unambiguous ones:
# "none" or "no" can be a correct answer to a question about what exists.
_PLACEHOLDERS = frozenset(
    {
        "idk",
        "i dont know",
        "dont know",
        "dunno",
        "no idea",
        "i have no idea",
        "no clue",
        "not sure",
        "i am not sure",
        "skip",
        "pass",
        "n/a",
        "na",
    }
)


def _normalize(text: str) -> str:
    text = text.lower().replace("\u2019", "'").replace("'", "")
    text = re.sub(r"[.!?\u2026\s]+$", "", text.strip())
    return re.sub(r"\s+", " ", text)


def _is_whole_word_in(word: str, context: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])", context) is not None


def is_non_answer(answer: str, context: str = "") -> bool:
    """True if `answer` attempts nothing. `context` is the code the question
    was grounded in; a short answer that appears in it as a whole word is
    left for the model to judge."""
    stripped = answer.strip()

    # Empty, or nothing but punctuation and whitespace.
    if not re.search(r"\w", stripped):
        return True

    # A run of one repeated letter: "xx", "xxx", "aaaa".
    if re.fullmatch(r"([A-Za-z])\1+[.!?]*", stripped):
        return True

    normalized = _normalize(stripped)
    if normalized in _PLACEHOLDERS:
        return not _is_whole_word_in(normalized, context)

    # One letter, nothing else ("x", "x.").
    short = re.fullmatch(r"([A-Za-z])[.!?]*", stripped)
    if short:
        return not _is_whole_word_in(short.group(1), context)

    return False
