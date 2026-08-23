from __future__ import annotations

from pathlib import Path

from viva.ingest.stack import detect_stack


def _write(root: Path, rel_path: str, content: str = "") -> Path:
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def test_detects_python_from_pyproject(tmp_path: Path) -> None:
    files = [_write(tmp_path, "pyproject.toml"), _write(tmp_path, "src/app/main.py")]

    assert detect_stack(files, tmp_path) == ["python"]


def test_detects_node_from_package_json(tmp_path: Path) -> None:
    files = [_write(tmp_path, "package.json"), _write(tmp_path, "src/index.js")]

    assert detect_stack(files, tmp_path) == ["node"]


def test_detects_multiple_manifests_in_a_polyglot_repo(tmp_path: Path) -> None:
    files = [
        _write(tmp_path, "backend/pyproject.toml"),
        _write(tmp_path, "frontend/package.json"),
    ]

    stack = detect_stack(files, tmp_path)
    assert set(stack) == {"python", "node"}


def test_django_framework_hint_added_alongside_python(tmp_path: Path) -> None:
    files = [
        _write(tmp_path, "requirements.txt"),
        _write(tmp_path, "manage.py"),
        _write(tmp_path, "app/models.py"),
    ]

    stack = detect_stack(files, tmp_path)
    assert stack == ["python", "django"]


def test_falls_back_to_extension_distribution_with_no_manifest(tmp_path: Path) -> None:
    files = [_write(tmp_path, f"src/f{i}.go") for i in range(29)]
    files.append(_write(tmp_path, "src/helper.py"))  # single stray file, below the 5% share threshold

    stack = detect_stack(files, tmp_path)
    assert stack == ["go"]


def test_extension_fallback_reports_multiple_languages_above_threshold(tmp_path: Path) -> None:
    files = [_write(tmp_path, f"src/f{i}.py") for i in range(5)]
    files += [_write(tmp_path, f"src/f{i}.js") for i in range(5)]

    stack = detect_stack(files, tmp_path)
    assert set(stack) == {"python", "javascript"}


def test_no_recognizable_stack_returns_empty_list(tmp_path: Path) -> None:
    files = [_write(tmp_path, "README.md"), _write(tmp_path, "data.csv")]

    assert detect_stack(files, tmp_path) == []


def test_manifest_signal_takes_priority_over_extensions(tmp_path: Path) -> None:
    # A python repo that happens to vendor a handful of .go files under a
    # non-manifest subdirectory shouldn't get "go" reported once a
    # manifest already resolved the stack.
    files = [_write(tmp_path, "pyproject.toml")]
    files += [_write(tmp_path, f"vendor_scripts/f{i}.go") for i in range(20)]

    assert detect_stack(files, tmp_path) == ["python"]
