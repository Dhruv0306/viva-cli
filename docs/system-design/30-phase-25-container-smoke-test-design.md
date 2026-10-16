# System Design Reference -- Part 30: Phase 25 Container Smoke Test Design

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. Implementation design for `docs/plan.md`
> Phase 25. Written against `main` at commit `931eae3` (Phase 24 merged;
> its `docker` job ran green on GitHub).

Phase 24's `docker` job proves the image builds and contains the right
files. It never starts the container. This phase adds the steps that do:
start the built image, read the access token from its log, call the web
server, and check how it is running. It is the automated version of what
Phase 22 did by hand on Windows, minus Ollama.

## 30.1 What is in and out

**In:** everything `viva serve` does before a session exists. That is the
part that broke in Phase 21 and 22 (entrypoint, ownership of the bind
mount, the token, the static files from the installed package), and it
needs no model.

**Out, on purpose:** starting a session. That needs Ollama and, for any
non-trivial repo, the ~22 MB tree-sitter grammar download. A CI job with a
model would be slow and flaky, and Phase 22's real runs already cover it.
`/api/sessions` is the one API call that touches storage without needing a
model, so it stands in for "the app is wired up".

## 30.2 Design

Two more steps at the end of the existing `docker` job (so the image built
there is reused with `docker compose up --no-build`):

1. `python3 scripts/smoke_container.py`, standard library only.
2. `docker compose down -v` with `if: always()`, as a backstop for a
   runner-level failure that skips the script's own cleanup.

The script starts the container, polls until `GET /` answers (90 s limit,
and it stops early if the container exits), and reads the token from the
log. Then:

| Check | Why |
| --- | --- |
| `GET /` returns 200 and the page does **not** contain the token | `/` is unauthenticated; embedding the secret there was a real bug (§21.6) |
| `GET /?token=<token>` embeds the token | the browser UI's way in |
| `GET /static/app.js` returns 200 JavaScript | static files served from the installed package, over HTTP |
| `GET /api/sessions` with no token and with a wrong token returns 401 | the container binds `0.0.0.0`, so the token must be enforced |
| `GET /api/sessions` with the token returns 200 and `[]` | the storage layer works with the bind-mounted `./data` |
| the Python process runs as `viva` | the entrypoint's privilege drop |
| `/app/data/viva.db` exists and is writable by `viva` | the ownership fix for the bind mount |
| the log has no `Traceback`, `PermissionError` or `Errno 13` | failures that do not surface as a bad status |

### 30.2.1 Decisions worth recording

- **The token is never printed.** It is random per container start, but a
  log is a log: every message and the failure log dump run through
  `redact()`, and a test pins that a wrong-token failure message contains
  neither token.
- **`docker compose exec` starts as root**, not as the server user, so
  `id` and a plain `test -w` would both pass for the wrong reason. The
  owner check reads `/proc` for the Python process, and the writability
  check uses `exec --user viva`. PID 1 is `su` (still root), which is why
  the probe looks at the Python process rather than PID 1.
- **The HTTP checks are a plain function over a base URL**, so a test can
  run them against a real viva app on a real socket (no Docker, no Ollama).
  A second test points them at a loopback-bound app, which has no token,
  and asserts the check fails. That is the mutation that matters here.
- **Failure output includes the container log**, masked, because the log is
  usually the answer and the runner is torn down afterwards.
- **No new `Config` field**, and no change to the application code.

## 30.3 Testing and exit criteria

- **Unit tests** (`tests/test_ci_scripts.py`, no Docker): token extraction
  from a real startup log, redaction, log scanning, owner-output parsing,
  the HTTP checks against a real in-process app (pass), with a wrong token
  (fail, nothing leaked) and against a loopback-bound app (fail).
- **Against the real server in the authoring sandbox:** `viva serve
  --host 0.0.0.0` run natively printed the token in the format the regex
  expects, answered every HTTP check above as designed and created
  `viva.db` on the first `/api/sessions` call.
- **Exit criteria:** full suite, `ruff` and `mypy` clean, then the `docker`
  job green on GitHub with the smoke steps added.

## 30.4 Out of scope

- A real session, Ollama, or the grammar download.
- Windows containers or Docker Desktop specifics; the job runs on
  `ubuntu-latest`.
- Checking `host.docker.internal` actually reaches an Ollama (the config
  check already asserts the mapping exists).

## 30.5 Implementation notes

8 new tests in `tests/test_ci_scripts.py` (24 in that file now): token
extraction (2), redaction, log scanning, owner-output parsing, and three
that run the HTTP checks against a real viva app on a real socket (pass,
wrong token, loopback bind with no token). Full suite 736 passed, 1
skipped in the authoring sandbox; `ruff` and `mypy` clean. The two server
tests share one `_running_app` helper.

The process-owner probe (`SERVER_USERS_SH`) was run natively in the
sandbox against a live `viva serve`: it finds the Python processes and
reports their owner, so the `/proc` parsing works. Inside the image the
result should be exactly `viva`.

**Not verified here:** the sandbox has no Docker daemon, so these have not
run: `docker compose up -d --no-build`, `docker compose ps --status
running -q`, `docker compose exec` (including `--user viva`), and the probe
inside the real image (it assumes `sh`, `tr`, `grep` and `stat` exist
there, which they do in Debian slim). The first run of the job with these
steps is their real test; a failure is a new patch on top, not a rebase.
This section gets a results entry once that run is seen.

