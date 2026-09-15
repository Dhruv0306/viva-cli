# System Design Reference — Part 21: Phase 15 `viva serve` Authentication Design

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. This is the design for `docs/plan.md`
> Phase 15, closing §19.4.1 from `19-panel-review-findings-2026-09.md`.
> Unlike Phase 14, this phase needs an explicit decision confirmed before
> any code is written — this doc makes a recommendation but the choice of
> section 21.2 is the thing to sign off on, not something to treat as
> already agreed. Written against `main` at commit `b9c3891` (the
> Phase 17 planning commit).

## 21.1 What's actually being revisited

`docs/system-design/15-phase-10-web-ui-design.md` §15.2 and §15.9 already
made a deliberate, reasoned call on this: no auth, binds to `127.0.0.1`
by default, "same trust boundary the CLI already has... not a hardened
multi-user service." That reasoning is sound for the default case and
this phase doesn't relitigate it — `viva serve` with no flags stays
exactly as fast and frictionless as it is today.

What §15.9 also says, in the same breath: *"`--host 0.0.0.0` remains
possible for someone who explicitly wants it, but isn't the default and
isn't hardened for it."* That's the gap. The flag exists, is one word to
type, and nothing between "I typed `--host 0.0.0.0`" and "anyone on this
network can start sessions, read reports, and clone repos under this
machine's `GITHUB_TOKEN`" currently happens. Phase 15's job is narrow:
close that specific gap without touching the well-reasoned default case
at all.

## 21.2 The decision

**Recommendation: a shared-secret bearer token, generated fresh per
`viva serve` invocation, enforced only when the bind address isn't
loopback.** Three options were weighed:

**A — Loud warning only, no real enforcement.** Require an
`--allow-remote` acknowledgment flag alongside `--host 0.0.0.0`, print a
warning, don't actually check anything. Cheapest to build, but it
protects against *accidental* exposure only — anyone who deliberately
wants unauthenticated LAN access just passes both flags. Given the
finding this phase exists to close is "anyone on the network can reach
an unauthenticated API," a warning that a determined user can silently
route around doesn't close it.

**B — Full session/login flow.** A username+password or cookie-session
system. Rejected on the project's own stated terms: `requirements.md`'s
single-user assumption and §15.2's explicit "no new abstractions where
an existing seam already does the job" bias. There's no multi-user
concept anywhere else in this codebase to hang a login flow off of, and
building one would be solving a problem (distinguishing between users)
this tool doesn't have, to protect against a threat (an unauthenticated
stranger on the LAN) that a much simpler mechanism already stops.

**C — Shared-secret bearer token (recommended).** A random token,
generated at `viva serve` startup, required on every `/api/*` request
once bound non-locally. This is real access control (unlike A: someone
without the token genuinely cannot reach the API, whether or not they
know a flag exists to bypass a warning) without inventing user accounts
(unlike B). It's also the smallest change that fits the existing
architecture — see §21.4, this is one middleware function and one
existing route's response body, not a new subsystem.

The rest of this doc designs C. Flag if A or B is actually what you want
before this moves to implementation.

## 21.3 Loopback detection

```python
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _requires_auth(host: str) -> bool:
    return host not in _LOOPBACK_HOSTS
```

Deliberately an explicit allowlist, not a "looks private" heuristic —
`0.0.0.0`, a real LAN IP, or anything else not in the set requires a
token. Fail-open-to-requiring-auth is the safer direction if this list
is ever incomplete; the reverse (fail-open-to-no-auth) is exactly the
gap this phase closes.

## 21.4 Wiring: `cli.py` and `create_app()`

`create_app()` currently only takes `config`. It gains an optional
`host` parameter, defaulting to the loopback address so every existing
call site (`create_app(config)` — 9 of them across `test_web_app.py`,
none passing a second argument) keeps today's no-auth behavior with zero
changes:

```python
def create_app(config: Config, host: str = "127.0.0.1") -> FastAPI:
    registry = SessionRegistry(config)
    require_token = _requires_auth(host)
    token = secrets.token_urlsafe(24) if require_token else None
    ...
```

`cli.py`'s `serve` command passes the real `host` through and prints the
token when one was generated:

```python
console.print(f"[green]Starting viva room on http://{host}:{port}[/green]")
app = create_app(config, host=host)
if app.state.viva_token:  # set below, in create_app -- see §21.5
    console.print(
        f"[yellow]Binding to a non-loopback address -- an access token "
        f"is required for every /api/* request.[/yellow]\n"
        f"[yellow]Token: {app.state.viva_token}[/yellow]\n"
        f"[yellow]Append ?token={app.state.viva_token} to the URL, or "
        f"send it as the X-Viva-Token header.[/yellow]"
    )
uvicorn.run(app, host=host, port=port)
```

Reading the token back off `app.state` rather than a second return value
from `create_app()` avoids changing that function's return type for a
detail only the console-printing step needs; `app.state` is the
conventional FastAPI place to stash per-app values set once at creation
time.

## 21.5 The middleware

One `@app.middleware("http")` function inside `create_app()`, added
right after the route definitions and before the `/static` mount so it's
visually adjacent to what it's protecting:

```python
app.state.viva_token = token  # None when require_token is False

@app.middleware("http")
async def _require_token(request: Request, call_next):
    if not require_token or not request.url.path.startswith("/api/"):
        return await call_next(request)
    supplied = request.headers.get("x-viva-token") or request.query_params.get("token")
    if supplied != token:
        return JSONResponse(status_code=401, content={"detail": "Missing or invalid access token."})
    return await call_next(request)
```

Scoped to `/api/*` only — `/`, `/favicon.ico`, and everything under
`/static/` stay reachable without a token even on a non-loopback bind.
Those routes serve the static app shell, no session data; gating them
too would just break the page load itself (the browser has no token to
attach when it automatically requests `/static/app.js` as a `<script
src>`, and the token has to come from *somewhere* the person can read
before any JS has run — see §21.6). Everything that actually touches
session data lives under `/api/*`, which is the boundary that matters.

`require_token`/`token` are closed over from `create_app()`'s scope, so
this is a single comparison per request when auth is off (the common
case) — no measurable cost added to the default, loopback path.

## 21.6 Getting the token into the browser: `index()` and `app.js`

`index()` currently returns `FileResponse(_STATIC_DIR / "index.html")`
unmodified. When a token is active, it needs to end up somewhere `app.js`
can read it before making its first `/api/*` call. No templating engine
exists in this codebase (§15.2's "no new frontend toolchain" — adding
Jinja2 for one variable would be disproportionate), so this is a plain
string substitution against a placeholder already sitting in
`index.html`:

```html
<!-- index.html, just before </head> -->
<script>window.__VIVA_TOKEN__ = "{{VIVA_TOKEN}}";</script>
```

```python
@app.get("/", include_in_schema=False)
def index() -> Response:
    html = (_STATIC_DIR / "index.html").read_text()
    html = html.replace("{{VIVA_TOKEN}}", token or "")
    return Response(content=html, media_type="text/html")
```

When `token` is `None` (the loopback/default case), this substitutes an
empty string — `window.__VIVA_TOKEN__ = "";` — and `app.js`'s check
below treats an empty string the same as "no token," so the default path
renders byte-for-byte the same page it does today in every way that
matters. (The route's return type changes from `FileResponse` to
`Response` since it's now constructing the body rather than streaming a
file; `favicon.ico` and the `/static` mount are untouched, they don't
carry the placeholder.)

`app.js` gets the token attached at the two points identified by
tracing every network call this file makes (§21.6.1):

**The central `api()` helper** (used by every JSON/text call — start,
resume, state polling, answer, session list, report view, cleanup):

```javascript
async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (window.__VIVA_TOKEN__) headers["X-Viva-Token"] = window.__VIVA_TOKEN__;
  const response = await fetch(path, { ...options, headers });
  ...
```

**`playQuestionAudio()`'s raw `fetch()`** (question-audio playback,
bypasses `api()` because it handles a `Blob`, not JSON/text):

```javascript
const response = await fetch(
  `/api/sessions/${encodeURIComponent(liveSessionId)}/question-audio`,
  window.__VIVA_TOKEN__ ? { headers: { "X-Viva-Token": window.__VIVA_TOKEN__ } } : {},
);
```

**The two report-download `<a href>` elements** (`report-download-md`,
`report-download-json`) — these are plain browser navigations, not
`fetch()` calls, so they can't carry a custom header at all; the token
has to go in the query string instead, which the middleware already
accepts as an alternative to the header:

```javascript
const tokenSuffix = window.__VIVA_TOKEN__ ? `&token=${encodeURIComponent(window.__VIVA_TOKEN__)}` : "";
document.getElementById("report-download-md").href =
  `${reportPath}?format=md&allow_partial=true&download=true${tokenSuffix}`;
document.getElementById("report-download-json").href =
  `${reportPath}?format=json&allow_partial=true&download=true${tokenSuffix}`;
```

### 21.6.1 Why these three and not more

Every network call in `app.js` was traced (`grep -n "fetch("` plus every
`.href =` assignment) to make sure nothing under `/api/*` is missed —
missing one wouldn't fail loudly, it would silently 401 and degrade
(e.g. `playQuestionAudio()`'s own `catch` block already swallows a
non-OK response and falls back to text-only, so a missed token there
would look like "voice mode is being flaky," not an obvious bug). The
HTML report *view* (as opposed to the two download links) goes through
`api()` like everything else and needed no separate handling.

## 21.7 Test plan

**`ingest`/`cli.py` — loopback detection and wiring:**
- `_requires_auth("127.0.0.1")`, `("localhost")`, `("::1")` all `False`.
- `_requires_auth("0.0.0.0")` and a real LAN-shaped IP both `True`.
- `viva serve` (default host) never prints a token line.
- `viva serve --host 0.0.0.0` prints a token line; the printed value
  matches `app.state.viva_token`.

**`web/app.py` — the middleware, via `TestClient`:**
- `create_app(config)` (no `host` arg — the existing 9 call sites'
  shape) still serves every route with no token required, full
  regression coverage via the existing test suite unmodified.
- `create_app(config, host="0.0.0.0")`: any `/api/*` request with no
  token, or the wrong token, returns `401`; the same request with the
  correct token (via `X-Viva-Token` header) returns its normal status;
  the same request with the correct token as a `?token=` query param
  also succeeds (covers the download-link case without actually
  exercising browser navigation in a Python test).
- `create_app(config, host="0.0.0.0")`: `GET /`, `GET /favicon.ico`, and
  `GET /static/app.js` all succeed with **no** token — confirms the
  `/api/` scoping in §21.5 is exact, not accidentally broader.
- `GET /` with `host="0.0.0.0"` contains the actual token value inside a
  `window.__VIVA_TOKEN__ = "..."` script tag; the same route with the
  default loopback host contains `window.__VIVA_TOKEN__ = "";` — proves
  both the injection and the "no behavior change on the default path"
  claim in §21.6, rather than just asserting the substitution ran.

**`web/static/app.js`:** still no test framework for this file (same
gap noted in Phase 14's doc). Manual verification needed: with a
non-loopback `viva serve` running, confirm the browser can actually
complete a full session (start → answer → report → cleanup) using only
the printed token, and confirm a **second** browser/incognito window
with no token (or a deliberately wrong `?token=` value) gets blocked
starting from the very first `/api/sessions` call, not partway through.

## 21.8 Explicitly out of scope

Matching §15.9's existing pattern of naming what a phase deliberately
doesn't do:

- **HTTPS.** The token travels in a header or query string over plain
  HTTP; on an untrusted network that's sniffable. Out of scope for the
  same reason §15.9 already puts HTTPS out of scope for the whole web
  UI — this is a LAN convenience tool, not a service meant to run
  across an untrusted network. Worth a one-line callout in `viva
  serve --help` or the console warning if that feels warranted, but not
  a TLS implementation.
- **Token persistence across restarts.** A fresh token every `viva
  serve` invocation, matching the existing "not a daemon" framing
  already in that command's docstring — no config file, no `--token`
  flag to pin a value, no rotation policy to design.
- **Per-route or per-user permissions.** The token is all-or-nothing
  access to `/api/*`, same as the CLI's own trust model (whoever has
  the token can do everything `viva serve`'s API exposes, the same way
  whoever can run `viva start` today can do everything the CLI exposes).
- **Rate limiting / brute-force protection on the token check.** A
  24-byte `secrets.token_urlsafe` value isn't guessable in any
  practically relevant timeframe; adding throttling here would be
  defending against a threat the token's own entropy already closes.
