"""Start the built viva container and check that it actually serves.

Phase 25 (docs/system-design/30-phase-25-container-smoke-test-design.md).
Run from the repo root after `docker compose build`, with `.env` in place.
Standard library only.

What it does: `docker compose up -d --no-build`, wait for the web server,
read the access token from the container's log, run HTTP and `docker
compose exec` checks, then always `docker compose down -v`. No Ollama and
no session are involved, so nothing here downloads a model or a
tree-sitter grammar.

The token is random per container start and lives only for this run, but
it is still never printed: it is masked in every message and in the log
dump shown on failure.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8000"
STARTUP_TIMEOUT_SECONDS = 90
TOKEN_RE = re.compile(r"Token: ([A-Za-z0-9_-]+)")
MASK = "***"


def extract_token(log: str) -> str | None:
    match = TOKEN_RE.search(log)
    return match.group(1) if match else None


def redact(text: str, token: str | None) -> str:
    if token:
        text = text.replace(token, MASK)
    return TOKEN_RE.sub(f"Token: {MASK}", text)


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Return (status, content_type, body); HTTP errors are results, not raises."""
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            body = response.read().decode("utf-8", errors="replace")
            return response.status, response.headers.get("Content-Type", ""), body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode("utf-8", errors="replace")


def run_http_checks(base_url: str, token: str) -> list[str]:
    """Return a list of problems; empty means every HTTP check passed.

    Messages never contain the token.
    """
    problems: list[str] = []

    status, _, body = _get(base_url + "/")
    if status != 200:
        problems.append(f"GET / returned {status}, expected 200")
    elif token in body:
        # The §21.6 bug: / is unauthenticated, so it must not embed the secret.
        problems.append("GET / without a token embedded the access token in the page")

    status, _, body = _get(f"{base_url}/?token={token}")
    if status != 200 or token not in body:
        problems.append("GET /?token=<token> did not embed the token for the browser UI")

    status, content_type, _ = _get(base_url + "/static/app.js")
    if status != 200 or "javascript" not in content_type:
        problems.append(f"GET /static/app.js returned {status} ({content_type!r}), expected 200 JavaScript")

    status, _, _ = _get(base_url + "/api/sessions")
    if status != 401:
        problems.append(f"GET /api/sessions without a token returned {status}, expected 401")

    status, _, _ = _get(base_url + "/api/sessions", {"X-Viva-Token": "not-the-token"})
    if status != 401:
        problems.append(f"GET /api/sessions with a wrong token returned {status}, expected 401")

    status, _, body = _get(base_url + "/api/sessions", {"X-Viva-Token": token})
    if status != 200:
        problems.append(f"GET /api/sessions with the token returned {status}, expected 200")
    else:
        try:
            sessions = json.loads(body)
        except json.JSONDecodeError:
            sessions = None
        if sessions != []:
            problems.append("GET /api/sessions with the token did not return an empty list")

    return problems


def check_log(log: str) -> list[str]:
    """Problems visible in the container log alone."""
    problems = []
    for needle in ("Traceback", "PermissionError", "Errno 13"):
        if needle in log:
            problems.append(f"container log contains {needle!r}")
    return problems


def _compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args], capture_output=True, text=True, check=check
    )


def _wait_until_serving() -> str | None:
    """Return None once GET / answers, else a description of what went wrong."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _compose("ps", "--status", "running", "-q", "viva", check=False).stdout.strip():
            return "the container is not running (it exited during startup)"
        try:
            if _get(BASE_URL + "/")[0] == 200:
                return None
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    return f"the server did not answer within {STARTUP_TIMEOUT_SECONDS}s"


# Prints the owning user of every process whose command starts with a
# Python interpreter. In the image that is exactly one process: the
# server, which `su` starts as `viva` (PID 1 is `su` itself, still root,
# and `docker compose exec` also starts as root, so neither `id` nor a
# plain exec can show who the server runs as).
SERVER_USERS_SH = (
    'for p in /proc/[0-9]*; do '
    'if tr "\\0" " " < "$p/cmdline" 2>/dev/null | grep -q "^[^ ]*python"; then '
    'stat -c %U "$p"; fi; done'
)


def parse_server_users(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]


def _exec_checks() -> list[str]:
    problems = []
    users = parse_server_users(
        _compose("exec", "-T", "viva", "sh", "-c", SERVER_USERS_SH, check=False).stdout
    )
    if users != ["viva"]:
        problems.append(
            f"python process owner(s) are {users!r}, expected ['viva'] (entrypoint privilege drop)"
        )
    # /api/sessions above opened the SQLite store, so the file must exist
    # and be writable by the non-root user: proves the ./data bind mount
    # ownership fix in entrypoint.sh worked. --user matters: exec starts
    # as root by default, for whom this test would always pass.
    db = _compose(
        "exec", "-T", "--user", "viva", "viva", "test", "-w", "/app/data/viva.db", check=False
    )
    if db.returncode != 0:
        problems.append("/app/data/viva.db does not exist or is not writable by the viva user")
    return problems


def main() -> int:
    token: str | None = None
    problems: list[str] = []
    try:
        up = _compose("up", "-d", "--no-build", check=False)
        if up.returncode != 0:
            print("FAIL: docker compose up failed:\n" + redact(up.stderr, None), file=sys.stderr)
            return 1
        failure = _wait_until_serving()
        log = _compose("logs", "--no-color", "viva", check=False)
        log_text = log.stdout + log.stderr
        token = extract_token(log_text)
        if failure:
            problems.append(failure)
        elif token is None:
            problems.append("no access token line in the container log")
        else:
            problems += run_http_checks(BASE_URL, token)
            problems += _exec_checks()
            log_text = _compose("logs", "--no-color", "viva", check=False)
            log_text = log_text.stdout + log_text.stderr
        problems += check_log(log_text)
        for problem in problems:
            print(f"FAIL: {redact(problem, token)}", file=sys.stderr)
        if problems:
            print("--- container log (token masked) ---", file=sys.stderr)
            print(redact(log_text, token), file=sys.stderr)
        else:
            print("container smoke test: OK")
        return 1 if problems else 0
    finally:
        _compose("down", "-v", check=False)


if __name__ == "__main__":
    raise SystemExit(main())
