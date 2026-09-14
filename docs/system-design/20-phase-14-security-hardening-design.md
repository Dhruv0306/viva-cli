# System Design Reference — Part 20: Phase 14 Security Hardening Design

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. This is the implementation design for
> `docs/plan.md` Phase 14, closing the five no-design-decision-needed
> items from `19-panel-review-findings-2026-09.md`. Written against `main`
> at commit `15338db` (the panel-review-findings-doc commit).

## 20.1 One fix, not three: unifying §19.4.2, §19.4.3, and §19.5.2

Reading the three findings together against the actual call graph changes
the shape of the fix. All three trace back to a single choke point:
`ingest/clone.py`'s `_repo_slug()`, and all three are closed by hardening
that one function rather than three separate patches.

**Current call graph.** `_repo_slug(repo_url)` is called exactly once,
inside `clone_repo()`, immediately before `_with_token()`. Every code path
that clones a repo goes through `clone_repo()` — the CLI's debug commands
(`viva ingest`/`viva analyze`/`viva index`/`viva questions`, `cli.py`
lines 146/189/262/354) call `ingest_repo()` directly, which calls
`clone_repo()`; `viva start`/`Orchestrator.start()` reaches the same
`clone_repo()` via `_run_setup_pipeline()`. There is no second, parallel
validation path to keep in sync — fixing `clone_repo()`'s own validation
is sufficient to close §19.4.2 (token host bypass) and §19.4.3
(unrestricted scheme) for every caller that exists today or gets added
later.

**§19.5.2 (reject garbage before a session row is persisted) is a
different concern** — not a security fix, a fail-fast one. `Orchestrator.
start()` calls `self.store.create_session(session_id, repo_url=repo_url,
...)` (orchestrator.py:162) *before* `_run_setup_pipeline()` ever reaches
`clone_repo()` (orchestrator.py:169), specifically so a crash mid-clone
still leaves a row `viva list` can show as `FAILED` (the comment at
orchestrator.py:159-161 states this explicitly, and it's the right
behavior for a real clone failure — auth, network, wrong branch). A
syntactically-invalid `repo_url` isn't that case: it's a request that
should never have been accepted in the first place, and doesn't deserve a
row. The fix is to call the *same* validation function early, inside
`Orchestrator.start()`, before `create_session()` — not a second,
independently-maintained shape check.

### 20.1.1 The bypass, precisely

`_SLUG_PATTERN` is anchored only at the end of the string:

```python
_SLUG_PATTERN = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<name>[^/.]+?)(?:\.git)?/?$")
```

`_repo_slug()` calls `.search()`, not `.fullmatch()`, so
`https://attacker.example/x/github.com/owner/repo` matches — the pattern
is found at the tail of the string — while `_with_token()`'s own
`urlsplit(repo_url).netloc` on that same string resolves to
`attacker.example`, not `github.com`. The two functions independently
inspect the same string in incompatible ways, and the token gets attached
to the wrong host.

### 20.1.2 The fix: one function, both checks, at the correct layer

Replace `_repo_slug()` with a function that validates and extracts the
slug in the same pass, using `urlsplit()`'s *parsed* host, not a
tail-anchored regex over the raw string. Repo URLs come in three shapes
this codebase already supports (see `_with_token()`'s existing comment
about SSH URLs "already carry their own auth"), so the replacement has to
handle all three, not just the `https://` one:

```python
_ALLOWED_URL_SCHEMES = frozenset({"https", "ssh"})
_URL_PATH_SLUG_PATTERN = re.compile(r"^/(?P<owner>[^/]+)/(?P<name>[^/.]+?)(?:\.git)?/?$")
_SCP_LIKE_PATTERN = re.compile(r"^[\w.-]+@github\.com:(?P<owner>[^/]+)/(?P<name>[^/.]+?)(?:\.git)?/?$")


def validate_repo_url(repo_url: str) -> str:
    """Validate `repo_url` is a github.com URL and return its owner/repo
    slug, in one pass -- see docs/system-design/19-panel-review-findings-
    2026-09.md \u00a719.4.2/\u00a719.4.3 for why this replaces the old
    `_repo_slug()`, which validated the raw string with a tail-anchored
    regex while `_with_token()` separately parsed it with `urlsplit()`;
    the two could disagree about the URL's actual host.

    Three accepted shapes, matching what `_with_token()` already assumes
    elsewhere in this module:
    - `https://github.com/owner/repo(.git)?`
    - `ssh://git@github.com/owner/repo(.git)?`
    - `git@github.com:owner/repo(.git)?` (SCP-like syntax; no scheme, so
      it's checked separately before `urlsplit()` is even consulted --
      `urlsplit()` doesn't parse this form as having a host at all).

    Raises CloneError for anything else, including a scheme outside
    {https, ssh} (closes \u00a719.4.3 -- nothing reaches `Repo.clone_from()`
    with an exotic transport like `ext::`) and a parsed host that isn't
    exactly `github.com` (closes \u00a719.4.2 -- a URL that merely contains
    the substring "github.com" no longer passes).
    """
    scp_match = _SCP_LIKE_PATTERN.match(repo_url)
    if scp_match:
        return f"{scp_match.group('owner')}/{scp_match.group('name')}"

    parts = urlsplit(repo_url)
    if parts.scheme in _ALLOWED_URL_SCHEMES and parts.hostname == "github.com":
        path_match = _URL_PATH_SLUG_PATTERN.match(parts.path)
        if path_match:
            return f"{path_match.group('owner')}/{path_match.group('name')}"

    raise CloneError(
        f"Could not derive a github.com owner/repo slug from {repo_url!r}; "
        "expected https://github.com/owner/repo, "
        "ssh://git@github.com/owner/repo, or git@github.com:owner/repo."
    )
```

Notes on why this closes the bypass specifically:
- `urlsplit().hostname` lowercases and strips any userinfo/port, and is
  compared with `==`, not `in`/`.search()` — `github.com.evil.com` (a
  suffix-match attack) and `attacker.example/.../github.com/...` (the
  original bypass, a substring-match attack) both fail the equality
  check.
- The scheme allowlist is checked independently of the host check, so a
  string like `ext::sh -c ... github.com/a/b` is rejected on the scheme
  alone (`urlsplit` parses `ext` as the scheme; `"ext" not in
  {"https", "ssh"}`) regardless of what appears later in the string. No
  payload needs to be constructed to verify this — it follows directly
  from the allowlist being checked before the regex ever runs.
- The SCP-like branch is matched with `^...$` (anchored both ends via
  `.match()` plus an explicit `$`), not `.search()`, so it can't be
  satisfied by a crafted suffix the way the old pattern could.

`_with_token()` gets one small hardening in the same patch, independent
of call order (defense in depth, in case a future caller reaches it
without going through `validate_repo_url()` first):

```python
def _with_token(repo_url: str, github_token: str | None) -> str:
    if not github_token:
        return repo_url
    parts = urlsplit(repo_url)
    if parts.scheme != "https" or parts.hostname != "github.com":
        # SSH/SCP URLs carry their own auth; and never attach a token to
        # a host that isn't exactly github.com, regardless of caller.
        return repo_url
    netloc = f"{github_token}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
```

(This changes the existing scheme check from `not in ("http", "https")`
to `!= "https"`, dropping plain `http` — nothing in this codebase clones
over unencrypted HTTP, and there's no reason to leave that door open.)

`clone_repo()` itself changes by one line, swapping the call:

```python
repo_slug = validate_repo_url(repo_url)   # was: _repo_slug(repo_url)
clone_url = _with_token(repo_url, github_token)
```

### 20.1.3 Wiring §19.5.2 through the same function

`Orchestrator.start()` (orchestrator.py) gains a validation call as its
first statement, before `uuid.uuid4()`:

```python
def start(
    self,
    repo_url: str,
    branch: str | None = None,
    duration_minutes: int | None = None,
    session_name: str | None = None,
) -> str:
    validate_repo_url(repo_url)  # raises CloneError early; see 20.1, 20.2
    if duration_minutes is not None and duration_minutes <= 0:
        raise InvalidParametersError(
            f"duration_minutes must be positive, got {duration_minutes}."
        )
    session_id = uuid.uuid4().hex[:12]
    effective_duration_minutes = (
        duration_minutes if duration_minutes is not None
        else self.config.viva_duration_minutes
    )
    ...
```

`validate_repo_url`'s return value (the slug) is discarded here — this
call exists purely to raise before any side effect happens, the real
slug gets derived again, cheaply, inside `clone_repo()` moments later.
Recomputing a regex match is not worth adding a parameter to thread the
slug through just to avoid it.

Both existing entry points get this for free with no changes of their
own beyond exception mapping (§20.3):
- `cli.py`'s `start` command already has `except CloneError as exc:
  raise typer.Exit(code=2)` — a `CloneError` raised here is caught
  exactly the same way a clone-time failure already is.
- `web/app.py`'s `start_session()` route needs one new `except CloneError`
  clause (it currently only has a blanket `except Exception -> 500`) —
  see §20.3.

## 20.2 `duration_minutes` validation (§19.3.1)

Two independent problems, per the original finding, both fixed in the
same `Orchestrator.start()` change shown above:

1. **The falsy-zero bug.** `duration_minutes or self.config.
   viva_duration_minutes` treats an explicit `0` the same as `None`.
   Replaced with `duration_minutes if duration_minutes is not None else
   self.config.viva_duration_minutes` — an explicit `0` is no longer
   silently substituted; it instead hits the new `<= 0` check below and
   is rejected outright, since a zero-minute viva isn't meaningful
   either.
2. **Negative values reaching the timer.** The new check,
   `if duration_minutes is not None and duration_minutes <= 0: raise
   InvalidParametersError(...)`, catches both `0` and negative values
   before `duration_seconds = float(effective_duration_minutes * 60)` is
   ever computed, so a negative `duration_seconds` can no longer reach
   `AnswerTimer`.

**New exception type.** `OrchestratorError` already has three subtypes
(`SessionNotFoundError`, `SessionAlreadyCompleteError`,
`SessionNotResumableError`), each representing a specific caller-facing
condition that both `cli.py` and `web/app.py` map to a specific exit
code / HTTP status ahead of the generic catch-all. `InvalidParametersError
(OrchestratorError)` follows the same pattern for "the caller passed a
bad value" as distinct from "something unexpected happened" — subclassing
`OrchestratorError` rather than raising a bare `ValueError` means it's
still catchable generically by anything that only cares about
orchestrator failures as a category, while both `cli.py` and `app.py` get
a specific `except InvalidParametersError` clause ahead of their existing
`except OrchestratorError` catch-all (Python's except-clause matching is
first-match, so ordering the specific clause first is what makes this
work, exactly like `CloneError` already sits ahead of the generic
`Exception` catch in `cli.py`'s `start` command today).

## 20.3 Exception-to-status-code wiring in `web/app.py`

`start_session()`'s current body:

```python
@app.post("/api/sessions")
def start_session(body: StartSessionRequest) -> dict:
    try:
        session_id = registry.start_session(
            body.repo_url, branch=body.branch,
            duration_minutes=body.duration_minutes, session_name=body.session_name,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"session_id": session_id}
```

Becomes:

```python
@app.post("/api/sessions")
def start_session(body: StartSessionRequest) -> dict:
    try:
        session_id = registry.start_session(
            body.repo_url, branch=body.branch,
            duration_minutes=body.duration_minutes, session_name=body.session_name,
        )
    except (CloneError, InvalidParametersError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"session_id": session_id}
```

matching the module's own documented contract ("2 (bad input) -> 400") at
the top of the file, and the identical pattern `resume_session()` already
uses one route below it for its own specific exception types. This relies
on `SessionRegistry._await_session_id()` re-raising the original
exception object from `error_holder` with its original type intact
(registry.py:236-244, `if "error" in error_holder: raise
error_holder["error"]`) — confirmed by reading that method; no change
needed there. `CloneError` and `InvalidParametersError` need importing
into `app.py` alongside the existing orchestrator-error imports.

`StartSessionRequest.duration_minutes` deliberately does **not** get a
Pydantic `Field(ge=1)` constraint, even though that would also reject
negative values. A Pydantic constraint failure produces FastAPI's
automatic `422 Unprocessable Entity` response, not the `400` this
module's own documented CLI-exit-code-to-HTTP-status mapping specifies,
and not the shape `cleanup()`'s existing `older_than` check already
established (`if body.older_than is not None and body.older_than <= 0:
raise HTTPException(status_code=400, ...)`, app.py:256-258). Validating
in the orchestrator and mapping the exception explicitly keeps every
"bad input" response in this API at the same status code, via the same
mechanism, rather than having some fields 422 and others 400 depending on
which layer happened to catch them.

## 20.4 Session-list stored XSS (§19.5.1)

**Current code**, `web/static/app.js:113-120`:

```javascript
const repoLabel = s.repo_slug || s.repo_url;
tr.innerHTML = `
  <td title="${s.session_id}">${shortId}</td>
  <td title="${repoLabel}">${repoLabel}</td>
  <td>${s.status}</td>
  <td>${s.updated_at}</td>
  <td></td>
`;
const actionCell = tr.lastElementChild;
```

`repo_url` is the raw string from `StartSessionRequest.repo_url`,
persisted regardless of whether cloning ever succeeds, and this is the
one call site in the file that reaches for `innerHTML` with a template
literal — every other dynamic value in `app.js` (23+ other sites) already
uses `.textContent`. The fix brings this site in line with the rest of
the file rather than introducing a new pattern:

```javascript
const repoLabel = s.repo_slug || s.repo_url;
const idCell = document.createElement("td");
idCell.title = s.session_id;
idCell.textContent = shortId;
const repoCell = document.createElement("td");
repoCell.title = repoLabel;
repoCell.textContent = repoLabel;
const statusCell = document.createElement("td");
statusCell.textContent = s.status;
const updatedCell = document.createElement("td");
updatedCell.textContent = s.updated_at;
const actionCell = document.createElement("td");
tr.append(idCell, repoCell, statusCell, updatedCell, actionCell);
```

`.title = value` is a property assignment, not markup, so it's inert the
same way `.textContent` is — no separate escaping needed for the `title`
attribute. Everything below `const actionCell = ...` (the `resumable`/
`COMPLETE` button logic, lines 121-145) is unchanged; it already operates
on `actionCell` as a DOM node via `createElement`/`appendChild`/
`textContent` and was never part of the vulnerable path.

## 20.5 Test plan

Each item gets a regression test confirmed to fail against pre-fix code
before the fix is trusted, per the usual pattern.

**`ingest/clone.py` (`validate_repo_url`, `_with_token`):**
- Three valid-shape cases (`https://`, `ssh://`, SCP-like) each resolve to
  the correct slug.
- `https://attacker.example/x/github.com/owner/repo` — the exact bypass
  shape from §19.4.2 — raises `CloneError`, and (separately, mocking
  `Repo.clone_from`) is confirmed to never construct a clone URL
  containing the token.
- A non-`{https,ssh}` scheme (`ext::...`, `file://...`) raises
  `CloneError` before `Repo.clone_from` is ever called (mock it and
  assert zero calls — this is the regression test proving §19.4.3, since
  there's no way to observe "an exotic transport didn't run" other than
  confirming the call it would have needed never happened).
- `github.com.evil.com` and a URL with a bogus owner/name shape (missing
  the repo segment, extra path segments) each raise `CloneError`.
- `_with_token` with a valid github.com URL still attaches the token
  (no regression on the working case); with any non-github.com host,
  confirmed to return the URL unchanged even if called directly,
  independent of `validate_repo_url`.

**`orchestrator.py` (`Orchestrator.start()`):**
- `duration_minutes=0` and `duration_minutes=-1` both raise
  `InvalidParametersError` before `self.store.create_session()` is called
  (mock or spy on `create_session` and assert zero calls — this is what
  actually proves no row gets persisted, not just that an exception was
  raised).
- A malformed `repo_url` (any of the `CloneError`-raising cases above)
  raises before `create_session()` is called, same assertion.
- `duration_minutes=None` still falls through to
  `self.config.viva_duration_minutes` (no regression on the default
  path).

**`web/app.py` (`POST /api/sessions`):**
- A malformed `repo_url` returns `400`, not `500`.
- `duration_minutes: 0` and `duration_minutes: -5` in the request body
  both return `400`.
- The existing happy-path test (valid repo, valid duration) still returns
  `200` with a `session_id` — confirms the new exception-mapping clause
  doesn't shadow the success path.

**`web/static/app.js` (session list rendering):**
- A `repo_url` of `<img src=x onerror=alert(1)>` renders as literal text
  in the table cell, not as a parsed `<img>` element — check via
  `tr.querySelector("img")` being `null` and the cell's `textContent`
  containing the raw string, in whatever JS test harness this repo
  already uses for `app.js` (or, if none exists yet, this is the natural
  place to add the first one, since Phase 10/12 didn't establish a
  frontend test pattern to follow).

## 20.6 Patch series order

Following the bisect-safe series convention (each commit leaves the full
suite passing), and grouping by file to keep review scope tight per
patch:

1. `ingest/clone.py` — `validate_repo_url()` replacing `_repo_slug()`,
   hardened `_with_token()`, updated `clone_repo()` call site, plus the
   clone.py test cases from §20.5. Closes §19.4.2 and §19.4.3 completely
   on its own, independent of the other patches.
2. `orchestrator.py` — the `Orchestrator.start()` changes (early
   `validate_repo_url()` call, `InvalidParametersError`, the falsy-zero
   fix), plus the orchestrator test cases from §20.5. Depends on patch 1
   for `validate_repo_url` to exist and be importable.
3. `web/app.py` — the new `except (CloneError, InvalidParametersError)`
   clause on `start_session()`, plus the API-level test cases from §20.5.
   Depends on patch 2 for `InvalidParametersError` to exist.
4. `web/static/app.js` — the XSS fix, independent of patches 1-3 (no
   Python-side dependency), can be reordered first or last without
   affecting the others.

Patches 1-3 are a true dependency chain (each needs the symbol the
previous one introduces); patch 4 is fully independent and included in
this series only because it's grouped into the same phase, not because
it needs to land in any particular position relative to the others.
