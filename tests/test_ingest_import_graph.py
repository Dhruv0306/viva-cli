"""Tests for viva.ingest.import_graph.

Uses synthetic trees with hand-crafted import statements so the expected
centrality of each file is known exactly, rather than relying on a
checked-in real repo where the "correct" centrality would itself need
independent verification.
"""
from __future__ import annotations

from pathlib import Path

from viva.ingest.import_graph import build_import_graph


def _write(root: Path, rel_path: str, content: str) -> Path:
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def test_python_imports_increase_target_centrality(tmp_path: Path) -> None:
    utils = _write(tmp_path, "src/app/utils.py", "def helper():\n    return 1\n")
    _write(tmp_path, "src/app/main.py", "from app.utils import helper\n\nhelper()\n")
    _write(tmp_path, "src/app/other.py", "import app.utils\n")

    files = [tmp_path / "src/app/utils.py", tmp_path / "src/app/main.py", tmp_path / "src/app/other.py"]
    graph = build_import_graph(files, tmp_path)

    assert graph.centrality(utils) == 2
    assert graph.centrality(tmp_path / "src/app/main.py") == 0


def test_javascript_relative_import_resolves(tmp_path: Path) -> None:
    models = _write(tmp_path, "src/models/user.js", "module.exports = {};\n")
    _write(tmp_path, "src/routes/handler.js", "const User = require('../models/user');\n")

    files = [tmp_path / "src/models/user.js", tmp_path / "src/routes/handler.js"]
    graph = build_import_graph(files, tmp_path)

    assert graph.centrality(models) == 1


def test_java_dotted_import_resolves(tmp_path: Path) -> None:
    util = _write(tmp_path, "com/acme/util/Helper.java", "package com.acme.util;\n")
    _write(
        tmp_path,
        "com/acme/Main.java",
        "package com.acme;\nimport com.acme.util.Helper;\n",
    )

    files = [tmp_path / "com/acme/util/Helper.java", tmp_path / "com/acme/Main.java"]
    graph = build_import_graph(files, tmp_path)

    assert graph.centrality(util) == 1


def test_go_import_block_is_parsed_not_every_string_literal(tmp_path: Path) -> None:
    pkg = _write(tmp_path, "internal/lib/thing.go", "package lib\n")
    _write(
        tmp_path,
        "cmd/main.go",
        'package main\n\nimport (\n\t"fmt"\n\t"myrepo/internal/lib"\n)\n\n'
        'func main() {\n\tfmt.Println("not an import")\n}\n',
    )

    files = [tmp_path / "internal/lib/thing.go", tmp_path / "cmd/main.go"]
    graph = build_import_graph(files, tmp_path)

    # "not an import" is a string literal inside main(), not an import
    # path -- it must not resolve to anything or inflate centrality.
    assert graph.centrality(pkg) == 1
    assert graph.centrality(tmp_path / "cmd/main.go") == 0


def test_ambiguous_reference_is_left_unresolved(tmp_path: Path) -> None:
    # Two different `utils.py` files under different modules -- a bare
    # `utils` reference can't be resolved unambiguously to either one, so
    # neither centrality should increase.
    a = _write(tmp_path, "moduleA/utils.py", "def a():\n    pass\n")
    b = _write(tmp_path, "moduleB/utils.py", "def b():\n    pass\n")
    _write(tmp_path, "moduleC/consumer.py", "import utils\n")

    files = [
        tmp_path / "moduleA/utils.py",
        tmp_path / "moduleB/utils.py",
        tmp_path / "moduleC/consumer.py",
    ]
    graph = build_import_graph(files, tmp_path)

    assert graph.centrality(a) == 0
    assert graph.centrality(b) == 0


def test_unreferenced_file_has_zero_centrality(tmp_path: Path) -> None:
    lonely = _write(tmp_path, "src/lonely.py", "x = 1\n")
    files = [lonely]

    graph = build_import_graph(files, tmp_path)

    assert graph.centrality(lonely) == 0


def test_self_import_does_not_self_count(tmp_path: Path) -> None:
    # Pathological but shouldn't crash or self-inflate: a file that
    # (nonsensically) imports itself by name.
    f = _write(tmp_path, "src/weird.py", "import weird\n")
    files = [f]

    graph = build_import_graph(files, tmp_path)

    assert graph.centrality(f) == 0
