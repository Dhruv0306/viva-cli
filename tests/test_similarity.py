"""Tests for viva.similarity (docs/system-design/32-phase-27-duplicate-
questions-design.md §32.2). The fixture is the 14 questions of one real
session against a one-function C file (viva-cli-test-c, October 2026),
which is the hard case: every question shares the same subject words."""
from __future__ import annotations

import itertools

import pytest

from viva.similarity import (
    DEFAULT_LEXICAL_DUPLICATE_THRESHOLD,
    is_lexical_duplicate,
    word_overlap,
)

REAL_SESSION = {
    "q_01": "In main, how is the loop's iteration count determined relative to the calculation performed by sum1ToN?",
    "q_02": "In main, what determines the upper bound for the loop iterating through the sum1ToN function?",
    "q_03": "What is the role of the `sum1ToN` function in calculating the output printed within the main loop?",
    "q_04": "Considering the `main` function, what is the hardcoded upper limit that controls the loop's iteration count?",
    "q_04_f1": "What is the specific purpose of the `sum1ToN` function when called within the `main` loop?",
    "q_10": "What specific calculation does the `sum1ToN` function perform when given an integer $n$?",
    "q_06": "In `main`, what value is passed as the argument `i` when calling `sum1ToN(i)`?",
    "q_07": "Why does the `main` function use a loop condition of `i < 11` instead of `i <= 10`?",
    "q_06_f1": "In `simple.c`, what mathematical formula does the `sum1ToN` function use to calculate the sum?",
    "q_05": "How does the `main` function use the return value from `sum1ToN(i)` to format the output string?",
    "q_08": "In `simple.c`, what is the return type of the `sum1ToN` function?",
    "q_09": "In `simple.c`, what is the specific calculation performed by the `sum1ToN` function when given an integer $n$?",
    "q_09_f1": "What is the return type of the `sum1ToN` function defined in `simple.c`?",
    "q_05_f1": "How does the `main` function utilize the return value from `sum1ToN(i)` to format the output string?",
}

# The pairs a human reads as the same question asked twice in nearly the
# same words. Paraphrases ("role" against "purpose") are the embedding
# layer's job and are not expected here.
NEAR_VERBATIM = {frozenset({"q_05", "q_05_f1"}), frozenset({"q_08", "q_09_f1"})}

# Genuine duplicates in different words: the same calculation asked twice.
# Word overlap scores this 0.50, too close to the false-positive side to
# gamble on, so it is deliberately left to the embedding layer.
PARAPHRASED = {frozenset({"q_10", "q_09"})}


def test_the_real_session_near_verbatim_duplicates_are_caught():
    for pair in NEAR_VERBATIM:
        a, b = sorted(pair)
        assert word_overlap(REAL_SESSION[a], REAL_SESSION[b]) >= DEFAULT_LEXICAL_DUPLICATE_THRESHOLD


def test_no_other_pair_in_the_real_session_is_called_a_duplicate():
    flagged = {
        frozenset({a, b})
        for a, b in itertools.combinations(REAL_SESSION, 2)
        if word_overlap(REAL_SESSION[a], REAL_SESSION[b]) >= DEFAULT_LEXICAL_DUPLICATE_THRESHOLD
    }
    assert flagged == NEAR_VERBATIM


def test_threshold_leaves_a_clear_margin_on_both_sides_for_this_session():
    scores = {
        frozenset({a, b}): word_overlap(REAL_SESSION[a], REAL_SESSION[b])
        for a, b in itertools.combinations(REAL_SESSION, 2)
    }
    lowest_duplicate = min(s for pair, s in scores.items() if pair in NEAR_VERBATIM)
    highest_other = max(
        s for pair, s in scores.items() if pair not in NEAR_VERBATIM | PARAPHRASED
    )
    assert lowest_duplicate - DEFAULT_LEXICAL_DUPLICATE_THRESHOLD >= 0.2
    assert DEFAULT_LEXICAL_DUPLICATE_THRESHOLD - highest_other >= 0.1


def test_identical_text_scores_one():
    assert word_overlap("What does retry do?", "What does retry do?") == 1.0


def test_unrelated_text_scores_zero():
    assert word_overlap("How is the cache invalidated?", "Which database holds sessions?") == 0.0


def test_text_with_no_topic_words_scores_zero_rather_than_dividing_by_zero():
    assert word_overlap("what is the", "how does it") == 0.0
    assert word_overlap("", "anything about retries") == 0.0


def test_identifiers_split_on_case_and_plurals_fold():
    assert word_overlap("Where is maxRetryCount used?", "Where is max_retry_count used?") == 1.0
    assert word_overlap("How are return values handled?", "How is the return value handled?") == 1.0


def test_is_lexical_duplicate_checks_every_earlier_question():
    asked = [REAL_SESSION["q_01"], REAL_SESSION["q_05"]]
    assert is_lexical_duplicate(REAL_SESSION["q_05_f1"], asked) is True
    assert is_lexical_duplicate(REAL_SESSION["q_07"], asked) is False
    assert is_lexical_duplicate(REAL_SESSION["q_05_f1"], []) is False


@pytest.mark.parametrize("threshold, expected", [(0.99, False), (0.1, True)])
def test_threshold_is_respected(threshold, expected):
    # q_05 / q_05_f1 overlap at about 0.9.
    assert is_lexical_duplicate(REAL_SESSION["q_05_f1"], [REAL_SESSION["q_05"]], threshold) is expected
