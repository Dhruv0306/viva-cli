"""Tests for viva.analyzer.extract -- AST extraction (FR6) and the
line-window fallback for unsupported/failed-parse files.
"""
from __future__ import annotations

from viva.analyzer.extract import analyze_file

WINDOW = 60
OVERLAP = 15


def test_python_function_and_class_extracted_with_docstring():
    code = '''
def add(a, b):
    """Adds two numbers."""
    return a + b

class Greeter:
    """Says hello."""
    def greet(self):
        return "hi"
'''
    result = analyze_file("src/x.py", code, module="src", line_window_size=WINDOW, line_window_overlap=OVERLAP)

    assert result.parse_method == "ast"
    assert result.language == "python"
    kinds = {(u.kind, u.name) for u in result.units}
    assert ("function", "add") in kinds
    assert ("class", "Greeter") in kinds
    assert ("function", "greet") in kinds

    add_unit = next(u for u in result.units if u.name == "add")
    assert add_unit.docstring == "Adds two numbers."
    assert add_unit.signature.startswith("def add(")


def test_javascript_uses_preceding_comment_as_docstring():
    code = """
// Adds two numbers
function add(a, b) {
  return a + b;
}
"""
    result = analyze_file("src/x.js", code, module="src", line_window_size=WINDOW, line_window_overlap=OVERLAP)

    assert result.parse_method == "ast"
    add_unit = next(u for u in result.units if u.name == "add")
    assert add_unit.docstring == "Adds two numbers"


def test_function_without_docstring_gets_body_excerpt():
    code = "def mystery(x):\n    y = x * 2\n    return y\n"
    result = analyze_file("src/x.py", code, module="src", line_window_size=WINDOW, line_window_overlap=OVERLAP)

    unit = result.units[0]
    assert unit.docstring is None
    assert "y = x * 2" in unit.body_excerpt


def test_unsupported_extension_falls_back_to_line_window():
    content = "\n".join(f"line {i}" for i in range(10))
    result = analyze_file("data/config.yaml", content, module="data", line_window_size=WINDOW, line_window_overlap=OVERLAP)

    assert result.parse_method == "line_window"
    assert result.language is None
    assert result.units == []
    assert len(result.raw_windows) == 1
    assert "line 0" in result.raw_windows[0]


def test_file_with_no_functions_or_classes_falls_back_to_line_window():
    # Valid Python, but nothing the query matches -- parses fine, just no
    # units, which should still fall back rather than return an empty AST
    # result the Map step would have nothing to summarize from.
    content = "CONSTANT = 42\nOTHER = 'value'\n"
    result = analyze_file("src/constants.py", content, module="src", line_window_size=WINDOW, line_window_overlap=OVERLAP)

    assert result.parse_method == "line_window"
    assert result.language == "python"
    assert result.raw_windows != []


def test_empty_file_produces_no_windows():
    result = analyze_file("src/empty.py", "", module="src", line_window_size=WINDOW, line_window_overlap=OVERLAP)

    assert result.parse_method == "line_window"
    assert result.raw_windows == []


def test_parse_failure_sets_parse_error_and_is_logged(monkeypatch, caplog):
    # Force _extract_ast_units to raise for an in-allowlist language, to
    # exercise the "real parse failure" path distinctly from "unsupported
    # extension" and "parsed fine but no units" -- all three fall back to
    # line_window, but only this one should set parse_error and log a
    # warning (this is exactly the signal that was missing when Windows
    # AST extraction silently fell back for every language at once).
    import viva.analyzer.extract as extract_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated grammar binary load failure")

    monkeypatch.setattr(extract_module, "_extract_ast_units", _boom)

    with caplog.at_level("WARNING"):
        result = extract_module.analyze_file(
            "src/x.py", "def f(): pass", module="src", line_window_size=WINDOW, line_window_overlap=OVERLAP
        )

    assert result.parse_method == "line_window"
    assert result.parse_error == "simulated grammar binary load failure"
    assert any("AST extraction failed" in r.message for r in caplog.records)


def test_no_units_found_does_not_set_parse_error():
    # Valid parse, just nothing the query matches -- distinct from an
    # actual failure, so parse_error must stay None.
    result = analyze_file("src/constants.py", "X = 1\n", module="src", line_window_size=WINDOW, line_window_overlap=OVERLAP)

    assert result.parse_method == "line_window"
    assert result.parse_error is None


def test_unsupported_extension_does_not_set_parse_error():
    result = analyze_file("data/config.yaml", "a: 1\n", module="data", line_window_size=WINDOW, line_window_overlap=OVERLAP)

    assert result.parse_error is None


def test_line_window_respects_size_and_overlap():
    # 100 lines, window 60, overlap 15 -> step 45 -> windows at [0:60], [45:100]
    content = "\n".join(f"line {i}" for i in range(100))
    result = analyze_file("data/big.txt", content, module="data", line_window_size=60, line_window_overlap=15)

    assert len(result.raw_windows) == 2
    assert "line 0" in result.raw_windows[0]
    assert "line 59" in result.raw_windows[0]
    assert "line 45" in result.raw_windows[1]
    assert "line 99" in result.raw_windows[1]


def test_go_method_and_struct_extracted():
    code = """
package main

func Add(a int, b int) int {
	return a + b
}

type Server struct{}

func (s *Server) Start() {}
"""
    result = analyze_file("main.go", code, module="", line_window_size=WINDOW, line_window_overlap=OVERLAP)

    assert result.parse_method == "ast"
    names = {u.name for u in result.units}
    assert {"Add", "Server", "Start"} <= names


def test_rust_function_and_struct_extracted():
    code = "fn add(a: i32, b: i32) -> i32 { a + b }\nstruct Point;\n"
    result = analyze_file("src/lib.rs", code, module="src", line_window_size=WINDOW, line_window_overlap=OVERLAP)

    names = {u.name for u in result.units}
    assert {"add", "Point"} <= names
