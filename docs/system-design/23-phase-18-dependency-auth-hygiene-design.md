# System Design Reference — Part 23: Phase 18 Dependency & Auth Hygiene Design

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. This is the implementation design for
> `docs/plan.md` Phase 18: three small, independent hygiene items found
> during an external deep-dive review, bundled into one phase the same
> way Phase 14 bundled multiple unrelated panel-review fixes. Written
> against `main` at commit `5ca2891` (the commit that added Phases 18-21
> to `plan.md`).

## 23.1 Dependency manifest drift

### 23.1.1 The `httpx2` typo

`pyproject.toml`'s `dev` extra lists:

```toml
dev = [
    "pytest>=8.0,<10.0",
    "pytest-mock>=3.14,<4.0",
    "httpx2>=2.0,<3.0",
]
```

`httpx2` is a real, unrelated PyPI package — a different maintainer,
different code, nothing to do with the `httpx` HTTP client. Confirmed by
downloading it directly (`pip download httpx2`) rather than assuming from
the name. `tests/test_web_app.py` uses `fastapi.testclient.TestClient`,
which requires the real `httpx` package at runtime. This works today only
because `ollama` — a direct base dependency — declares `httpx>=0.27` in
its own `Requires-Dist` (confirmed by inspecting the wheel metadata), so
`pip install -e ".[dev]"` ends up with real `httpx` installed as a side
effect of an unrelated package, while `httpx2` sits in the environment
doing nothing. If `ollama`'s own dependency on `httpx` is ever dropped or
its version floor moved in a way that conflicts with what `TestClient`
needs, `test_web_app.py` starts failing with an import error that has
nothing obviously to do with the actual change that triggered it.

### 23.1.2 `requirements.txt` is missing `fastapi` and `uvicorn` entirely

Found while tracing this phase's scope rather than in the original
review. `requirements.txt`'s base section:

```
typer>=0.12,<1.0
rich>=13.7,<16.0
pydantic>=2.6,<3.0
python-dotenv>=1.0,<2.0
ollama>=0.3.0,<1.0
GitPython>=3.1,<4.0
tree-sitter>=0.23,<1.0
tree-sitter-language-pack>=0.7,<2.0
chromadb>=0.5,<2.0
```

`pyproject.toml`'s `dependencies` list has all of the above *plus*
`prompt_toolkit`, `fastapi`, and `uvicorn`. `prompt_toolkit` is only used
by the CLI's live-answer input (`session_ui.py`) and its absence from
`requirements.txt` is a pre-existing, separate gap not in this phase's
scope. `fastapi` and `uvicorn` are `viva serve`'s entire runtime — the
README's own Installation section documents `pip install -r
requirements.txt` as the path, meaning anyone following the README
exactly gets an install that raises `ModuleNotFoundError: No module named
'fastapi'` the first time they run `viva serve`, despite the command
being fully documented later in the same README's Usage section.
`CONTRIBUTING.md`'s dev setup uses `pip install -e ".[dev]"` instead,
which is why this hasn't surfaced through the contributor path — only
through the plain end-user install path the README documents first.

### 23.1.3 Fix: sync both manifests explicitly, no new abstraction

Two manifests already exist for two different audiences (`requirements.txt`
for a plain end-user install per the README, `pyproject.toml` + extras for
contributors per `CONTRIBUTING.md`), and collapsing them into one is a
bigger change than this phase's scope — it would mean rewriting the
README's Installation section and deciding whether end users are expected
to know about extras syntax at all. That's a legitimate follow-up
question but not this phase's; noted in §23.7 rather than folded in here.

For this phase, both manifests get corrected to actually match what they
already claim to provide:

- `pyproject.toml`: `"httpx2>=2.0,<3.0"` → `"httpx>=0.27,<1.0"` in the
  `dev` extra. Floor matches `ollama`'s own declared requirement rather
  than an arbitrary newer version, since that's the actual constraint
  already implicitly in effect.
- `requirements.txt`: add `fastapi>=0.115,<1.0` and `uvicorn>=0.30,<1.0`
  to the base section (exact same version ranges as `pyproject.toml`,
  copied rather than re-decided), and add `httpx>=0.27,<1.0` to the
  `# dev/test` section alongside `pytest`/`pytest-mock`, for the same
  reason as the `pyproject.toml` fix — it's a direct test-time dependency
  of `test_web_app.py`, not an incidental one, and should be declared
  where it's used rather than relying on `ollama` to keep supplying it
  transitively.

No version-pinning tool (`pip-compile`, `poetry`, a lockfile) is being
introduced here — that would be a bigger, independent decision (see
§23.7) and isn't needed to close either gap.

## 23.2 Constant-time token comparison

`web/app.py`'s `_require_token` middleware (added in Phase 15):

```python
supplied = request.headers.get("x-viva-token") or request.query_params.get("token")
if supplied != token:
    return JSONResponse(status_code=401, content={"detail": "Missing or invalid access token."})
```

Not a response to a demonstrated exploit — `token` is a 192-bit
`secrets.token_urlsafe(24)` value generated per `viva serve` invocation,
and a network-observable timing attack against Python's `!=` on strings
of that length is not a practical threat here. This is defense-in-depth:
`hmac.compare_digest` is the correct primitive for any secret comparison,
independent of whether a specific attack is currently feasible, and using
it costs nothing.

**Fix:**

```python
import hmac
# ...
supplied = request.headers.get("x-viva-token") or request.query_params.get("token")
if not hmac.compare_digest(supplied or "", token):
    return JSONResponse(status_code=401, content={"detail": "Missing or invalid access token."})
```

`compare_digest` requires both arguments to be the same type (`str`/`str`
or `bytes`/`bytes`) and raises on `None`, so `supplied or ""` guards the
common case where no header or query param was sent at all — this
preserves current behavior (missing token → 401) rather than raising an
unhandled exception. `token` itself is never `None` inside this branch,
since the middleware's outer `if not require_token` check already returns
early when `token` is `None` (the loopback/default case).

## 23.3 LICENSE

`pyproject.toml` currently has `license = { text = "TBD" }`; the README's
License section just says "TBD." The repo is public and `CONTRIBUTING.md`
actively invites contributions, but with no LICENSE file, default
copyright applies and nothing grants anyone — including a contributor
sending a PR — a clear right to use, modify, or redistribute the code.

**Decision: MIT.** Fits the project's own stated posture (no API keys,
nothing leaves the user's machine, freely usable/forkable) and is the
least-friction choice for a project this early-stage that's already
soliciting outside contributions. This is the one item in this phase
that's a genuine decision rather than a mechanical fix, so it's called
out explicitly for sign-off before the patch lands, per the usual
"agree before code" pattern for anything that isn't purely mechanical —
even though the stakes here are low, licensing is the kind of thing worth
a deliberate yes rather than a default.

**Fix:**
- New `LICENSE` file at repo root, standard MIT text, copyright line
  `Copyright (c) 2026 Dhruv Patel`.
- `pyproject.toml`: `license = { text = "TBD" }` → `license = { text = "MIT" }`.
- README's License section: `TBD.` → `MIT — see [LICENSE](LICENSE).`

## 23.4 Documentation update plan

This phase is unusual in that most of its diff *is* documentation or
metadata rather than application code — called out explicitly since that
was asked for directly. Every file touched, grouped by which patch in
§23.6 introduces the change:

| File | Change | Patch |
|---|---|---|
| `pyproject.toml` | `httpx2` → `httpx` in `dev` extra; `license` field → MIT | 1, 3 |
| `requirements.txt` | add `fastapi`, `uvicorn` to base; add `httpx` to dev/test | 1 |
| `src/viva/web/app.py` | `hmac.compare_digest` swap | 2 |
| `LICENSE` | new file, MIT text | 3 |
| `README.md` | License section: `TBD` → `MIT` + link | 3 |
| `CHANGELOG.md` | `[Unreleased]` entry covering all three fixes | 4 |
| `docs/plan.md` | Phase 18 entry gets a `**Verified**` line once real-world validation (§23.5) completes | 4 |

Two files deliberately **not** touched by this phase, with reasoning:

- `docs/system-design/21-phase-15-serve-authentication-design.md` — the
  original Phase 15 design doc stays as-written; design docs are a
  historical record of the decision as it was made (`ways-of-working.md`:
  "forward-only patching," bugs get a new numbered patch/doc on top, not
  a retrofit of the original). This doc (Part 23) is where the
  `compare_digest` hardening is recorded.
- `CONTRIBUTING.md` — its dev-setup instructions already use
  `pip install -e ".[dev]"`, which is unaffected by either manifest fix
  (it was already getting `httpx` transitively via `ollama`, and it was
  already getting `fastapi`/`uvicorn` from `pyproject.toml`'s base
  dependencies, not `requirements.txt`). Nothing in it is inaccurate
  today.

## 23.5 Test plan

**`pyproject.toml` / `requirements.txt` (manifest fixes):** no code to
regression-test — a dependency manifest isn't a code path. Verification
is real-world only:
- Fresh venv, `pip install -e ".[dev]"`: `pip show httpx` succeeds,
  `pip show httpx2` fails (confirms it's gone, not just that `httpx` was
  added alongside it); `pytest -q` passes in full.
- Separate fresh venv, `pip install -r requirements.txt` (the README's
  documented path, not `pip install -e .`): `python -c "import fastapi,
  uvicorn"` succeeds; `viva serve --help` runs without an import error.

**`web/app.py` (`hmac.compare_digest`):** behavior-preserving change, so
per §23.1's own logic this isn't a "regression test written against
failing pre-fix code" case — the pre-fix code doesn't fail any existing
behavior, it just uses a weaker comparison primitive to reach the same
result. The test asserts the *un-changed* observable behavior still
holds after the swap:
- Correct token (header or query param) → 200/expected response.
- Wrong token → 401.
- No token at all → 401 (exercises the `supplied or ""` guard against
  `hmac.compare_digest` receiving `None`).
- Existing Phase 15 test suite (`test_web_app.py`'s token tests) re-run
  in full and confirmed still green — this is the actual regression
  check, on the existing tests rather than a new one.

**LICENSE:** not code, no automated test. Real-world validation:
`pip install .` from a clean checkout and confirm the built wheel/sdist
metadata reports `License: MIT` (`pip show viva-cli` after install).

## 23.6 Patch series order

Bisect-safe (full suite green after each commit), grouped so each patch
has a single clear reason to exist:

1. **Dependency manifests** — `pyproject.toml`'s `httpx2`→`httpx` fix and
   `requirements.txt`'s `fastapi`/`uvicorn`/`httpx` additions, together
   (both are pure manifest edits, no code, no reason to split). Verified
   via the two fresh-venv installs in §23.5 before moving on — this is
   the one patch in the series where "does it even install cleanly" is
   the actual test.
2. **`web/app.py`** — the `hmac.compare_digest` swap, plus the
   behavior-preserving test additions from §23.5. Independent of patch 1;
   could be reordered first without consequence.
3. **LICENSE** — new `LICENSE` file, `pyproject.toml`'s `license` field,
   README's License section. Depends on patch 1 only in the sense that
   it touches the same file (`pyproject.toml`) — no functional
   dependency, ordered last among the three fixes because it's the one
   item needing explicit sign-off (§23.3) before it lands, so it's the
   natural patch to hold if sign-off is still pending while 1 and 2 ship.
4. **`CHANGELOG.md` + `docs/plan.md`** — the `[Unreleased]` entry and the
   Phase 18 `**Verified**` line, added last, once patches 1-3 are merged
   and §23.5's real-world validation has actually been run — matching
   the existing convention that a phase's "Verified" note in `plan.md`
   records what was actually confirmed, not what's expected to work.

## 23.7 Deferred, not in scope for this phase

- **Collapsing `requirements.txt` and `pyproject.toml` into one manifest.**
  Legitimate question raised by §23.1.2, but bigger than a hygiene fix —
  it means deciding whether end users are expected to use extras syntax,
  and rewriting the README's Installation section either way. Backlog
  candidate, not blocking this phase.
- **A lockfile / pinned-versions tool** (`pip-compile`, `poetry`, `uv
  lock`). Would prevent this exact class of drift from recurring, but is
  a tooling adoption decision for the whole project, not a two-line fix —
  natural fit for Phase 19 (CI Quality Gates) to raise alongside `ruff`/
  `mypy`, not this phase.
- **`prompt_toolkit`'s absence from `requirements.txt`**, noted in
  §23.1.2 — same class of bug as the `fastapi`/`uvicorn` gap, but out of
  this phase's original scope (found while writing this doc, not part of
  the original review). Flagging here rather than silently expanding
  patch 1 further; worth confirming whether it's fixed alongside 1 or
  gets its own follow-up — small enough that folding it into patch 1 is
  reasonable if there's no objection.

## 23.9 Real-world correction: httpx2 was not a typo

Found while validating patch 1 (fresh-venv installs, per §23.5), before
patches 3 and 4 shipped. §23.1.1 and the original review it came from are
left as written above rather than edited in place, per the project's own
"not silently fixed" convention for real-world corrections — this section
is the record of what was actually found and what changed as a result.

**What was claimed:** that `httpx2` in `pyproject.toml`'s `dev` extra was
a typo for `httpx`, an unrelated package installed for no reason.

**What's actually true:** `httpx2` is [Pydantic's actively maintained
fork of `httpx`](https://github.com/pydantic/httpx2), with `httpx`'s
original author involved. `httpx` itself has had no release since 2024.
Starlette's `TestClient` (which `tests/test_web_app.py` uses via
`fastapi.testclient.TestClient`) now imports `httpx2` first and only
falls back to plain `httpx` with a `StarletteDeprecationWarning`; the
Starlette maintainer's own reasoning, from the PR that added this:
`httpx` "has become somehow unmaintained... and pydantic/httpx2 is the
least annoying path forward for every consumer of that package." FastAPI,
the OpenAI SDK, the Anthropic SDK, and the MCP SDK have made the same
move. Confirmed directly: running this project's test suite against an
environment with only `httpx` installed (no `httpx2`) produces exactly
that deprecation warning; installing `httpx2` instead removes it, with
the full suite passing clean either way.

**What was actually wrong, once this was corrected:** only what §23.1.2
already identified independently — `requirements.txt`'s dev/test section
never had `httpx2` *or* `httpx` in it at all, so
`fastapi.testclient.TestClient` had nothing to import when installing via
`pip install -r requirements.txt` rather than `pip install -e ".[dev]"`.
`pyproject.toml`'s original `httpx2>=2.0,<3.0` line was correct as
written and needed no change.

**Corrected patch 1 content** (supersedes the `pyproject.toml` /
`requirements.txt` rows in §23.4's table and the patch 1 description in
§23.6):
- `pyproject.toml`: **no change** — `httpx2>=2.0,<3.0` stays as-is.
- `requirements.txt`: add `fastapi>=0.115,<1.0` and `uvicorn>=0.30,<1.0`
  to the base section (unchanged from the original plan), and add
  `httpx2>=2.0,<3.0` — not `httpx` — to the `# dev/test` section, matching
  `pyproject.toml`'s already-correct choice rather than replacing it.

**Process note:** this is exactly the kind of thing the original review
should have caught by checking what `httpx2` actually was rather than
stopping at "a different package exists with this name, therefore
probably a typo." Confirming a package exists is not the same as
confirming what it's for — worth remembering for future dependency
findings, not just this one.
