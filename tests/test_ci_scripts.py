"""Tests for the Docker CI helper scripts (Phase 24, docs/system-design/
29-phase-24-docker-ci-job-design.md).

`scripts/` is not a package, so the scripts are loaded by path. The
inputs are shaped like real `docker compose config --format json` output
(captured from compose v5 against this repo's docker-compose.yml), so the
checks are exercised against realistic data without needing Docker.
"""
from __future__ import annotations

import contextlib
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


# -- scripts/smoke_container.py (Phase 25) ---------------------------------

smoke = _load("smoke_container")


def test_extract_token_from_a_real_startup_log() -> None:
    log = (
        "Starting viva room on http://0.0.0.0:8000\n"
        "Binding to a non-loopback address -- an access token is required for every \n"
        "/api/* request.\n"
        "Token: ZBPa48LcL6HsDrehvgBETtwL74GCgmXg\n"
        "Append ?token=ZBPa48LcL6HsDrehvgBETtwL74GCgmXg to the URL, or send it as the \n"
    )
    assert smoke.extract_token(log) == "ZBPa48LcL6HsDrehvgBETtwL74GCgmXg"


def test_extract_token_returns_none_without_a_token_line() -> None:
    assert smoke.extract_token("INFO: Uvicorn running on http://0.0.0.0:8000\n") is None


def test_redact_masks_the_token_everywhere() -> None:
    text = "Token: abc123\nAppend ?token=abc123 to the URL\nstray abc123"
    redacted = smoke.redact(text, "abc123")
    assert "abc123" not in redacted
    assert "Token: ***" in redacted


def test_check_log_flags_tracebacks_and_permission_errors() -> None:
    assert smoke.check_log("INFO: fine\n") == []
    assert len(smoke.check_log("Traceback (most recent call last):\n")) == 1
    assert any("PermissionError" in p for p in smoke.check_log("PermissionError: [Errno 13] x"))


@contextlib.contextmanager
def _running_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bind_host: str):
    """A real viva web app on a real socket, with no Ollama anywhere.

    `bind_host` is what viva is told it is bound to (a non-loopback value
    makes it require a token, as in the container); the socket itself
    always listens on 127.0.0.1.
    """
    import socket
    import threading
    import time

    import uvicorn

    from viva.config import Config
    from viva.web.app import create_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "viva.db"))
    monkeypatch.setenv("VECTOR_DB_PATH", str(tmp_path / "chroma"))
    app = create_app(Config.load(), host=bind_host)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}", app.state.viva_token
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with _running_app(tmp_path, monkeypatch, "0.0.0.0") as running:
        yield running


def test_http_checks_pass_against_the_real_app(served) -> None:
    base_url, token = served
    assert smoke.run_http_checks(base_url, token) == []


def test_http_checks_fail_when_the_token_is_wrong(served) -> None:
    base_url, token = served
    problems = smoke.run_http_checks(base_url, "not-the-real-token")
    assert any("did not embed the token" in p for p in problems)
    assert any("with the token returned 401" in p for p in problems)
    assert all("not-the-real-token" not in p for p in problems)
    assert all(token not in p for p in problems)


def test_parse_server_users_ignores_blank_lines() -> None:
    assert smoke.parse_server_users("viva\n\n") == ["viva"]
    assert smoke.parse_server_users("") == []
    assert smoke.parse_server_users("root\nviva\n") == ["root", "viva"]


def test_http_checks_notice_a_server_that_does_not_require_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loopback bind has no token; if the container ever did that, /api/*
    would be open to anything that can reach the port, and the check must fail."""
    with _running_app(tmp_path, monkeypatch, "127.0.0.1") as (base_url, _):
        problems = smoke.run_http_checks(base_url, "some-token")
    assert any("without a token returned 200, expected 401" in p for p in problems)
