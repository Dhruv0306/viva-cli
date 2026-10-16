"""Check the resolved docker-compose config for known Docker setup bugs.

Reads the JSON printed by `docker compose config --format json` on stdin
and exits non-zero, listing every problem, if the `viva` service is
misconfigured. Phase 24 (docs/system-design/29-phase-24-docker-ci-job-
design.md).

The CI job runs this with the project's `.env` copied from
`.env.example` and `OLLAMA_HOST=http://localhost:11434` set in the shell,
which is the exact situation that broke Phase 22's first container run
(§27.10). The input is never echoed: `docker compose config` prints
GITHUB_TOKEN in plain text, and so would any message that quoted it.
"""
from __future__ import annotations

import json
import sys
from typing import Any

SERVICE = "viva"
EXPECTED_OLLAMA_HOST = "http://host.docker.internal:11434"
EXPECTED_PORT = 8000
EXPECTED_EXTRA_HOST = "host.docker.internal=host-gateway"


def check_config(config: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems; empty means the config is fine."""
    problems: list[str] = []
    service = config.get("services", {}).get(SERVICE)
    if service is None:
        return [f"no '{SERVICE}' service in the resolved config"]

    ollama_host = service.get("environment", {}).get("OLLAMA_HOST")
    if ollama_host != EXPECTED_OLLAMA_HOST:
        problems.append(
            "OLLAMA_HOST inside the container is "
            f"{ollama_host!r}, expected {EXPECTED_OLLAMA_HOST!r} "
            "(the native OLLAMA_HOST must not leak into the container, "
            "design doc 27 section 27.10)"
        )

    ports = service.get("ports", [])
    published = [p for p in ports if int(p.get("target", 0)) == EXPECTED_PORT]
    if not published:
        problems.append(f"container port {EXPECTED_PORT} is not published at all")
    for port in published:
        if port.get("host_ip") != "127.0.0.1":
            problems.append(
                f"container port {EXPECTED_PORT} is published on host_ip "
                f"{port.get('host_ip')!r}, expected '127.0.0.1' "
                "(loopback only, design doc 27 section 27.7)"
            )

    if EXPECTED_EXTRA_HOST not in service.get("extra_hosts", []):
        problems.append(
            f"extra_hosts lacks {EXPECTED_EXTRA_HOST!r}; native Linux cannot "
            "resolve host.docker.internal without it (design doc 26 section 26.2)"
        )

    mounts = {v.get("target") for v in service.get("volumes", [])}
    for target in ("/app/data", "/home/viva/.cache/tree-sitter-language-pack"):
        if target not in mounts:
            problems.append(f"expected a volume mounted at {target}")

    return problems


def main() -> int:
    try:
        config = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Deliberately no input in the message (it may contain GITHUB_TOKEN).
        print("could not parse `docker compose config` output as JSON", file=sys.stderr)
        return 2
    problems = check_config(config)
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if not problems:
        print("docker compose config: OK")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
