"""Tests for the Docker CI helper scripts (Phase 24, docs/system-design/
29-phase-24-docker-ci-job-design.md).

`scripts/` is not a package, so the scripts are loaded by path. The
inputs are shaped like real `docker compose config --format json` output
(captured from compose v5 against this repo's docker-compose.yml), so the
checks are exercised against realistic data without needing Docker.
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compose_check = _load("check_compose_config")
image_check = _load("check_image_assets")


def _good_config() -> dict:
    return {
        "services": {
            "viva": {
                "environment": {
                    "EMBEDDING_MODEL": "nomic-embed-text",
                    "GITHUB_TOKEN": "",
                    "LLM_MODEL": "gemma4:e4b",
                    "MAX_CONCURRENT_SESSIONS": "5",
                    "OLLAMA_HOST": "http://host.docker.internal:11434",
                },
                "ports": [
                    {
                        "mode": "ingress",
                        "host_ip": "127.0.0.1",
                        "target": 8000,
                        "published": "8000",
                        "protocol": "tcp",
                    }
                ],
                "extra_hosts": ["host.docker.internal=host-gateway"],
                "volumes": [
                    {"type": "bind", "source": "/x/data", "target": "/app/data"},
                    {
                        "type": "volume",
                        "source": "tree_sitter_cache",
                        "target": "/home/viva/.cache/tree-sitter-language-pack",
                    },
                ],
            }
        }
    }


def test_good_compose_config_has_no_problems() -> None:
    assert compose_check.check_config(_good_config()) == []


def test_native_ollama_host_leaking_into_the_container_is_caught() -> None:
    config = _good_config()
    config["services"]["viva"]["environment"]["OLLAMA_HOST"] = "http://localhost:11434"
    problems = compose_check.check_config(config)
    assert len(problems) == 1
    assert "OLLAMA_HOST" in problems[0]


def test_port_published_on_all_interfaces_is_caught() -> None:
    config = _good_config()
    del config["services"]["viva"]["ports"][0]["host_ip"]
    problems = compose_check.check_config(config)
    assert len(problems) == 1
    assert "127.0.0.1" in problems[0]


def test_unpublished_port_is_caught() -> None:
    config = _good_config()
    config["services"]["viva"]["ports"] = []
    assert any("not published" in p for p in compose_check.check_config(config))


def test_missing_volume_is_caught() -> None:
    config = _good_config()
    config["services"]["viva"]["volumes"].pop()
    problems = compose_check.check_config(config)
    assert any("tree-sitter-language-pack" in p for p in problems)


def test_missing_host_gateway_mapping_is_caught() -> None:
    config = _good_config()
    del config["services"]["viva"]["extra_hosts"]
    problems = compose_check.check_config(config)
    assert len(problems) == 1
    assert "host.docker.internal" in problems[0]


def test_missing_service_is_caught() -> None:
    assert compose_check.check_config({"services": {}}) == [
        "no 'viva' service in the resolved config"
    ]


def test_compose_main_never_echoes_its_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "ghp_not_a_real_token"
    config = _good_config()
    config["services"]["viva"]["environment"]["GITHUB_TOKEN"] = secret
    config["services"]["viva"]["environment"]["OLLAMA_HOST"] = "http://localhost:1"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(config)))
    assert compose_check.main() == 1
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


def test_compose_main_on_garbage_input_fails_without_echoing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("GITHUB_TOKEN=ghp_secret not json"))
    assert compose_check.main() == 2
    captured = capsys.readouterr()
    assert "ghp_secret" not in captured.out + captured.err


def _make_package(root: Path, *, drop: str | None = None) -> Path:
    installed = root / "site-packages" / "viva"
    (installed / "web" / "static").mkdir(parents=True)
    (installed / "analyzer" / "queries").mkdir(parents=True)
    for name in image_check.STATIC_FILES:
        (installed / "web" / "static" / name).write_text("x")
    for name in ("python.scm", "java.scm"):
        (installed / "analyzer" / "queries" / name).write_text("x")
    if drop:
        (installed / drop).unlink()
    return installed


def _make_source_queries(root: Path) -> Path:
    queries = root / "src" / "viva" / "analyzer" / "queries"
    queries.mkdir(parents=True)
    for name in ("python.scm", "java.scm"):
        (queries / name).write_text("x")
    return queries


def test_complete_installed_package_has_no_problems(tmp_path: Path) -> None:
    installed = _make_package(tmp_path)
    assert image_check.find_problems(installed, _make_source_queries(tmp_path)) == []


def test_missing_static_file_is_caught(tmp_path: Path) -> None:
    installed = _make_package(tmp_path, drop="web/static/app.js")
    problems = image_check.find_problems(installed, _make_source_queries(tmp_path))
    assert problems == ["missing viva/web/static/app.js in the installed package"]


def test_missing_query_file_is_caught(tmp_path: Path) -> None:
    installed = _make_package(tmp_path, drop="analyzer/queries/java.scm")
    problems = image_check.find_problems(installed, _make_source_queries(tmp_path))
    assert problems == ["missing viva/analyzer/queries/java.scm in the installed package"]


def test_new_source_query_file_is_covered_automatically(tmp_path: Path) -> None:
    installed = _make_package(tmp_path)
    queries = _make_source_queries(tmp_path)
    (queries / "rust.scm").write_text("x")
    problems = image_check.find_problems(installed, queries)
    assert problems == ["missing viva/analyzer/queries/rust.scm in the installed package"]


def test_source_tree_install_is_caught(tmp_path: Path) -> None:
    installed = tmp_path / "app" / "src" / "viva"
    (installed / "web" / "static").mkdir(parents=True)
    (installed / "analyzer" / "queries").mkdir(parents=True)
    problems = image_check.find_problems(installed, _make_source_queries(tmp_path))
    assert any("not from site-packages" in p for p in problems)


def test_empty_source_queries_dir_does_not_pass_vacuously(tmp_path: Path) -> None:
    installed = _make_package(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    problems = image_check.find_problems(installed, empty)
    assert any("vacuously" in p for p in problems)


def test_real_source_tree_has_query_files_for_the_check_to_compare() -> None:
    queries = Path(__file__).resolve().parent.parent / "src" / "viva" / "analyzer" / "queries"
    assert list(queries.glob("*.scm")), "the CI image check compares against these files"
