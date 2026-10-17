"""Cheap, model-free similarity between two questions (docs/plan.md
Phase 27, docs/system-design/32-phase-27-duplicate-questions-design.md
§32.2).

The embedding check in the Orchestrator (FR15's third layer) needs the
embedding model and a threshold that depends on it. This is the layer under
it: word overlap, which needs neither. It is deliberately conservative. It
catches a question that is a near-verbatim rephrase of one already asked
("How does main use the return value from sum1ToN(i) to format the output
string?" against "...utilize the return value..."), and it does not catch
paraphrases ("role" against "purpose"); that is the embedding layer's job.
"""
from __future__ import annotations

import re

# Words that carry no topic. Generic English question scaffolding only:
# nothing here is specific to a repository.
_STOPWORDS = frozenset(
    "the a an of in is to what how does do and or on its that this with from as it be are "
    "into within when given by for instead specific which why would should can will used "
    "use using function method code".split()
)

# Phase 27 §32.2: measured on the 14 questions of one real session (a
# one-function C file, where every question shares the same subject words,
# which is the hard case). The two near-verbatim rephrases scored 0.90 and
# 0.86; the best score of any pair that is NOT a duplicate was 0.44; one
# genuine duplicate pair ("what calculation does sum1ToN perform", asked
# two ways) scored 0.50. 0.6 sits well clear of the false-positive side and
# leaves that last pair to the embedding layer rather than gambling on a
# 0.06 margin.
DEFAULT_LEXICAL_DUPLICATE_THRESHOLD = 0.6


def _tokens(text: str) -> frozenset[str]:
    # camelCase and snake_case identifiers are split so `sum1ToN` and
    # `sum_1_to_n` do not hide a shared subject from each other.
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    words = re.findall(r"[a-z0-9]+", spaced.lower())
    # Crude plural folding; enough for "return values" against "return value".
    return frozenset(w.rstrip("s") if len(w) > 4 else w for w in words if w not in _STOPWORDS)


def word_overlap(a: str, b: str) -> float:
    """Jaccard overlap of the topic words of `a` and `b`, 0.0 to 1.0."""
    tokens_a, tokens_b = _tokens(a), _tokens(b)
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def is_lexical_duplicate(
    candidate: str,
    asked: list[str],
    threshold: float = DEFAULT_LEXICAL_DUPLICATE_THRESHOLD,
) -> bool:
    """True if `candidate` overlaps any already-asked question at or above
    `threshold`."""
    return any(word_overlap(candidate, earlier) >= threshold for earlier in asked)
