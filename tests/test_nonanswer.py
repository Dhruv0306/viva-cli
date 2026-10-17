"""Tests for viva.nonanswer (docs/system-design/32-phase-27-duplicate-
questions-design.md §32.5). The context is the real simple.c the Phase 26
session asked about: `sum1ToN` and a loop over `i`, no `x`."""
from __future__ import annotations

import pytest

from viva.nonanswer import is_non_answer

SIMPLE_C = """[simple.c:1-13]
int sum1ToN(int n) {
    return n * (n + 1) / 2;
}
int main(void) {
    for (int i = 1; i < 11; i++) {
        printf("Sum of 1 to %d is %d\\n", i, sum1ToN(i));
    }
}"""


@pytest.mark.parametrize(
    "answer",
    ["x", "xx", "X", "x.", "xxx", "aaaa", "", "   ", "...", "?!", "-", "idk", "IDK", "I don't know",
     "I don\u2019t know.", "dont know", "no idea", "Not sure", "skip", "n/a", "N/A"],
)
def test_attempts_that_say_nothing_are_non_answers(answer):
    assert is_non_answer(answer, SIMPLE_C) is True


@pytest.mark.parametrize(
    "answer",
    [
        "n",                      # a variable in the code, a plausible real answer
        "i",
        "11",
        "42",
        "O(n)",
        "int",
        "It loops from 1 to 10.",
        "The return value is passed to printf.",
        "n*(n+1)/2",
        "no",                     # could be a real answer to a yes/no question
        "none",
        "x + 1",
        "i < 11",
    ],
)
def test_real_attempts_go_to_the_model(answer):
    assert is_non_answer(answer, SIMPLE_C) is False


def test_a_short_answer_that_is_a_word_in_the_code_is_left_to_the_model():
    assert is_non_answer("i", SIMPLE_C) is False
    assert is_non_answer("n", SIMPLE_C) is False
    assert is_non_answer("x", SIMPLE_C) is True
    assert is_non_answer("x", "int x = 3; return x;") is False


def test_whole_word_not_substring_decides_it():
    # `x` is inside "max" and "index" but is not the word `x`.
    assert is_non_answer("x", "int max = index + 1;") is True


def test_a_placeholder_that_is_itself_in_the_code_is_left_to_the_model():
    assert is_non_answer("pass", "def f():\n    try:\n        run()\n    except E:\n        pass") is False
    assert is_non_answer("pass", SIMPLE_C) is True


def test_without_context_one_letter_is_a_non_answer_but_a_sentence_is_not():
    assert is_non_answer("x") is True
    assert is_non_answer("It returns the sum.") is False
