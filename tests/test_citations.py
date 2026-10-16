"""Tests for viva.citations (docs/system-design/31-phase-26-followups-
design.md §31.5)."""
from __future__ import annotations

import pytest

from viva.citations import normalize_citation

CONTEXT = (
    "[src/payments/handler.py:40-80]\ndef charge(order): ...\n\n---\n\n"
    "[Calculator.java:1-30]\npublic class Calculator { }"
)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "N/A",
        "n/a",
        "N/A (Not present in the policy file)",
        "(N/A)",
        "None",
        "none.",
        "null",
        "Not present in the code",
        "Not applicable",
        "not found in the provided context",
        "No citation available",
        "No specific file",
        "Unknown",
        "-",
        "...",
    ],
)
def test_text_that_says_there_is_no_citation_is_rejected(raw):
    assert normalize_citation(raw) is None
    assert normalize_citation(raw, CONTEXT) is None


@pytest.mark.parametrize(
    "raw",
    [
        "src/payments/handler.py:42",
        "src/payments/handler.py",
        "handler.py:50-60",
        "Calculator.java:12",
        "src\\payments\\handler.py:42",
    ],
)
def test_a_citation_naming_a_file_in_the_context_is_kept(raw):
    assert normalize_citation(raw, CONTEXT) == raw


def test_a_citation_is_returned_stripped():
    assert normalize_citation("  Calculator.java:12  ", CONTEXT) == "Calculator.java:12"


@pytest.mark.parametrize(
    "raw",
    ["src/billing/invoice.py:10", "README.md", "Invoice.java:3"],
)
def test_a_citation_to_a_file_the_model_was_never_shown_is_rejected(raw):
    assert normalize_citation(raw, CONTEXT) is None


def test_without_context_only_the_no_citation_check_applies():
    assert normalize_citation("src/billing/invoice.py:10") == "src/billing/invoice.py:10"


def test_a_bare_function_name_is_kept_only_if_it_appears_in_the_context():
    assert normalize_citation("charge", CONTEXT) == "charge"
    assert normalize_citation("charge()", CONTEXT) == "charge()"
    assert normalize_citation("refund", CONTEXT) is None


def test_empty_context_grounds_nothing():
    assert normalize_citation("src/payments/handler.py:42", "") is None


def test_na_prefix_does_not_reject_a_real_path_that_starts_with_those_letters():
    context = "[nanny/service.py:1-5]\npass"
    assert normalize_citation("nanny/service.py:2", context) == "nanny/service.py:2"
    assert normalize_citation("notes/readme.md", "[notes/readme.md:1-2]\nx") == "notes/readme.md"
