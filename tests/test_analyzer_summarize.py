from __future__ import annotations

from unittest.mock import MagicMock

from viva.analyzer.models import CodeUnit, FileAnalysis
from viva.analyzer.summarize import summarize_files


def _mock_llm():
    llm = MagicMock()
    llm.summarize_file.return_value = "a summary"
    return llm


def test_summarize_files_calls_llm_once_per_file():
    analyses = [
        FileAnalysis(path="a.py", module="", language="python", parse_method="ast", units=[]),
        FileAnalysis(path="b.py", module="", language="python", parse_method="ast", units=[]),
    ]
    llm = _mock_llm()

    summaries = summarize_files(analyses, llm)

    assert len(summaries) == 2
    assert llm.summarize_file.call_count == 2
    assert all(s.summary == "a summary" for s in summaries)


def test_ast_excerpt_includes_docstring_when_present():
    unit = CodeUnit(
        kind="function", name="add", signature="def add(a, b):", docstring="Adds two numbers.",
        start_line=1, end_line=2,
    )
    analysis = FileAnalysis(path="a.py", module="src", language="python", parse_method="ast", units=[unit])
    llm = _mock_llm()

    summarize_files([analysis], llm)

    excerpt = llm.summarize_file.call_args.kwargs["content_excerpt"]
    assert "Adds two numbers." in excerpt
    assert "def add(a, b):" in excerpt


def test_ast_excerpt_falls_back_to_body_when_no_docstring():
    unit = CodeUnit(
        kind="function", name="mystery", signature="def mystery(x):", docstring=None,
        start_line=1, end_line=3, body_excerpt="y = x * 2\nreturn y",
    )
    analysis = FileAnalysis(path="a.py", module="src", language="python", parse_method="ast", units=[unit])
    llm = _mock_llm()

    summarize_files([analysis], llm)

    excerpt = llm.summarize_file.call_args.kwargs["content_excerpt"]
    assert "y = x * 2" in excerpt


def test_line_window_excerpt_uses_first_two_windows_only():
    analysis = FileAnalysis(
        path="a.txt", module="", language=None, parse_method="line_window",
        raw_windows=["window one", "window two", "window three"],
    )
    llm = _mock_llm()

    summarize_files([analysis], llm)

    excerpt = llm.summarize_file.call_args.kwargs["content_excerpt"]
    assert "window one" in excerpt
    assert "window two" in excerpt
    assert "window three" not in excerpt


def test_file_summary_carries_module_and_parse_method_through():
    analysis = FileAnalysis(path="a.py", module="src/app", language="python", parse_method="ast", units=[])
    llm = _mock_llm()

    [summary] = summarize_files([analysis], llm)

    assert summary.path == "a.py"
    assert summary.module == "src/app"
    assert summary.parse_method == "ast"
