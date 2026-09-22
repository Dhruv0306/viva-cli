# System Design Reference — Part 25: Phase 20 Serve Hardening & Onboarding Design

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. Implementation design for `docs/plan.md`
> Phase 20. Written against `main` at commit `c6d2e14` (Phase 18/19 both
> complete). Two unrelated items bundled the way Phase 18 bundled three:
> both touch the `viva serve` / first-run path, neither depends on the
> other. Every claim about how `SessionRegistry` and `ollama.Client`
> actually behave below was checked directly against this codebase and a
> live (if deliberately unreachable) Ollama client, not assumed --
> `plan.md`'s original Phase 20 bullet turned out to have two real gaps
> once checked, corrected in §25.1 rather than carried forward silently.

## 25.1 Rate limiting: two corrections to the original plan

`plan.md`'s Phase 20 entry proposed "a per-token in-memory counter...
capping concurrent `IN_PROGRESS` sessions started by the same token."
Two things about that turned out wrong once checked against
`web/registry.py` and `web/app.py` directly, not assumed from the
bullet's own wording:

**"Per-token" isn't a real dimension.** `create_app()` generates exactly
one token per `viva serve` process
(`token = secrets.token_urlsafe(24) if require_token else None`,
`app.py:87`), shared by everyone who has it -- there's no per-caller
identity anywhere in this system beyond that single shared secret.
"Per-token" and "per-process" are the same thing here. The cap is a
process-wide concurrent-session limit, not a per-caller one; the design
below reflects that rather than repeating the original framing.

**`len(self._sessions)` isn't "concurrent."** `SessionRegistry._sessions`
(a `dict[str, LiveSession]`) never removes an entry once added --
confirmed directly (`grep` for `del`/`.pop()` against `_sessions` in
`registry.py`: no hits). A session that finished an hour ago is still in
that dict. Naively capping on `len(self._sessions)` would mean the server
becomes permanently unusable after a handful of *completed* sessions,
not actually protect against concurrent load. What the cap actually
needs is the count of entries whose background thread is still alive:
`LiveSession.thread.is_alive()`. Traced this end to end --
`Orchestrator.start()`/`.resume()` is a single blocking call that runs
setup, the full live Q&A loop (`ui.read_answer()` blocks the thread
between HTTP requests, per `web_session_ui.py`), and finalization all on
one thread; the thread only exits once the whole session is done,
whether it succeeded or raised. So `thread.is_alive()` genuinely tracks
"is this session still consuming resources right now" -- not just "was
this session ever started."

## 25.2 Rate limiting: design

**Scope: both `start_session()` and `resume_session()`**, not just
session creation. Both spawn a live background thread and add an entry
to `self._sessions`; both are the same resource-exhaustion vector the
original `plan.md` bullet was concerned about (each one holds an
`Orchestrator` that can be mid-`ollama` call at any point), even though
`resume` doesn't re-clone the repo. Narrowing the cap to only
`start_session()` would leave an obvious way around it.

**New `SessionRegistry` method:**

```python
def active_session_count(self) -> int:
    with self._lock:
        return sum(1 for live in self._sessions.values() if live.thread.is_alive())
```

**New exception + check**, raised at the top of both `start_session()`
and `resume_session()`, before a `SessionStore`/`Orchestrator`/thread is
created (no point spending any of that setup cost only to reject the
request):

```python
class TooManyActiveSessionsError(Exception):
    """Raised by SessionRegistry when the concurrent-session cap
    (Config.max_concurrent_sessions) is already at capacity."""
```

```python
if self.active_session_count() >= self._config.max_concurrent_sessions:
    raise TooManyActiveSessionsError(
        f"{self._config.max_concurrent_sessions} session(s) already "
        "active on this server. Wait for one to finish, or restart "
        "viva serve with a higher MAX_CONCURRENT_SESSIONS."
    )
```

**`app.py` mapping**, mirroring the existing
`except (CloneError, InvalidParametersError)` /
`except (SessionAlreadyCompleteError, SessionNotResumableError)` pattern
already in both endpoints (§ app.py:100-125): a new
`except TooManyActiveSessionsError as exc: raise HTTPException(status_code=429, detail=str(exc)) from exc`
added to both `start_session` and `resume_session`'s existing `try`
blocks -- one new `except` clause each, no restructuring.

**Loopback (no token) case is unaffected in spirit, not in mechanism.**
`plan.md`'s original bullet said "the cap only applies when
`require_token` is true" -- worth re-examining given §25.1's correction,
since the cap is no longer framed as per-token. Decision: the cap
applies **unconditionally**, loopback included. The original carve-out's
reasoning (a solo loopback user shouldn't be rate-limited against
themselves) still has merit, but a fixed default high enough to never
matter for normal single-user CLI-adjacent use (see default below) makes
a loopback-specific carve-out unnecessary complexity for no real
behavior difference in practice, and keeps one code path instead of two.

**Config:** new field `max_concurrent_sessions: int`, env var
`MAX_CONCURRENT_SESSIONS`, default `"5"` -- via the existing
`_get_positive_int(name, default)` helper (`config.py:33`), same pattern
as `MAX_QUESTIONS`/`SESSION_RETENTION_DAYS`/`MAX_FILES`. Default raised
from the original bullet's "e.g. 3" to 5: a single interactive user can
plausibly have 2-3 browser tabs open against different sessions (the web
UI doesn't prevent this today), and the failure mode of setting it too
low is a confusing 429 for a legitimate solo user, while the failure mode
of setting it too high (on a single-user tool) is a slower server under
genuine abuse -- asymmetric enough to lean toward the higher default.

**`Config` field ripple:** confirmed directly -- 11 test files construct
`Config(...)` with every field spelled out (no inline defaults on the
dataclass, by design, per `ways-of-working.md`). Adding
`max_concurrent_sessions` ripples across all 11, same as
`MAX_RETRIEVAL_DISTANCE` did in Phase 16.

## 25.3 `viva doctor`

New, separate, read-only CLI command -- no changes to `viva start`/
`serve` themselves. Three checks, run in order, each short-circuiting the
ones after it that depend on its success:

**1. Config loads.** `Config.load()`, same call `cleanup`/`start`/every
other command already makes. If it raises `ConfigError` (most commonly:
`LLM_MODEL` not set), print the same message `Config.load()` already
produces and exit 2 -- `viva doctor` doesn't need its own version of this
message, `Config.load()`'s existing one already says exactly what to do
("Copy .env.example to .env and set LLM_MODEL..."). This *is* one of the
three onboarding steps `plan.md`'s Phase 20 bullet names, it's just
already handled by existing infrastructure rather than needing new code.

**2. Ollama reachable.** `ollama.Client(host=config.ollama_host,
timeout=5.0).list()` -- 5s, not `OllamaClient`'s 120s generation
timeout (`llm_client.py:337`), since this is a read-only reachability
probe that should either succeed almost instantly or fail fast, not a
real generation call. **Real finding, checked directly against a live
client, not assumed:** the `ollama` package's `.list()` call raises two
*different* exception types depending on the failure mode. "Connection
refused" (Ollama not running at all -- the common case) is wrapped by
`ollama`'s own code into a friendly plain `ConnectionError` with a
built-in "Please check that Ollama is downloaded, running and
accessible" message. A network-level timeout (Ollama host set but
unreachable) raises `httpx.ConnectTimeout` *unwrapped* -- confirmed by
actually connecting to a non-routable address, not inferred. Catching
only `ConnectionError` would miss the second case entirely and produce
an unhandled traceback instead of a clean doctor report. Fix: catch
bare `Exception` around this one call specifically (not the broader
command), since the goal here is "tell the user Ollama isn't reachable
and why," and enumerating every possible `httpx` exception subclass to
`ollama`'s own internal choice of client is a coupling this command
shouldn't take on -- str(exc) already gives a reasonable reason in both
observed cases.

**3 & 4. `LLM_MODEL`/`EMBEDDING_MODEL` pulled.** Only run if check 2
succeeded (no `ListResponse` to check against otherwise). Match
`config.llm_model`/`config.embedding_model` against
`{m.model for m in response.models}`. Known limitation, noted rather
than solved here: Ollama model tags carry an implicit `:latest` suffix
in some contexts and not others (e.g. a model pulled as `llama3` may
list as `llama3:latest`) -- an exact-string match could false-negative
on a tag-suffix mismatch that isn't actually a real problem. Worth a
real user hitting this before adding suffix-normalization logic on
spec; flagged in §25.5 rather than guessed at now.

**Output**, one line per check, Rich-formatted matching the rest of
`cli.py`'s conventions (`cleanup`'s `[green]`/`[red]` style):

```
✓ Configuration loaded (LLM_MODEL=gemma4:e4b, EMBEDDING_MODEL=nomic-embed-text)
✓ Ollama reachable at http://localhost:11434
✓ LLM_MODEL 'gemma4:e4b' is pulled
✗ EMBEDDING_MODEL 'nomic-embed-text' is not pulled -- run: ollama pull nomic-embed-text
```

Exit 0 if every check passes, 1 otherwise (matching `cleanup`'s exit-2-
for-bad-input/exit-1-for-runtime-failure split isn't quite right here --
`doctor`'s failures are all "environment isn't ready," not "you gave me
a bad flag," so a flat 0/1 split is the right level of granularity, not
`cleanup`'s three-way one).

## 25.4 Documentation update plan

| File | Change |
|---|---|
| `src/viva/web/registry.py` | `active_session_count()`, `TooManyActiveSessionsError`, the check in both `start_session()`/`resume_session()` |
| `src/viva/web/app.py` | new `except TooManyActiveSessionsError` clause in both endpoints |
| `src/viva/config.py` | new `max_concurrent_sessions` field |
| `src/viva/cli.py` | new `doctor` command |
| 11 test files | `Config(...)` construction ripple (§25.2) |
| `README.md` | "Usage" section gets a `viva doctor` entry alongside the other commands; "Project status" gets a Phase 20 bullet once verified |
| `CHANGELOG.md` | `[Unreleased]` entry once implemented and verified |
| `docs/plan.md` | Phase 20 entry gets its `**Verified**` line; the "per-token"/`len(self._sessions)` corrections in §25.1 get an appended corrected note there too, same treatment as Phase 18's `httpx2` correction, since the original bullet is what a future reader would otherwise trust |

## 25.5 Test plan / exit criteria

- Rate limit: a test asserting the `(cap + 1)`th concurrent
  `start_session()` call from a set of sessions whose threads are still
  genuinely alive (not just present in the dict -- the test needs to
  actually block those threads, e.g. via a mocked `Orchestrator.start()`
  that waits on an event, to distinguish this from the
  `len(self._sessions)` bug this design avoids) is rejected with
  `TooManyActiveSessionsError` / HTTP 429; a session finishing (thread
  exits) and a subsequent request succeeding, proving the cap tracks
  liveness, not historical count. Confirmed failing against pre-fix code
  first. Real-world run against `viva serve --host 0.0.0.0` with
  `MAX_CONCURRENT_SESSIONS=1` confirms the same behavior live.
- `viva doctor`: unit tests mocking `ollama.Client.list()` to raise
  `ConnectionError`, to raise a bare timeout-shaped exception (covering
  §25.3's real finding -- a test that only mocks `ConnectionError` would
  miss exactly the gap that finding exists to close), and to succeed
  with/without the configured models present; asserting the correct exit
  code and per-check message in each case. Real-world run: Ollama
  stopped, then running with a model missing, then fully correct,
  confirming each state reports accurately.

## 25.6 Deferred, not in scope for this phase

- **Model-tag suffix normalization** (`:latest` mismatches, §25.3) --
  noted, not solved; revisit once a real user hits it rather than
  guessing at the right normalization now.
- **A loopback-specific carve-out for the concurrency cap** (§25.2) --
  decided against for this phase; revisit only if the flat default
  genuinely proves too restrictive for real single-user use, which the
  raised default (5, up from the original bullet's "e.g. 3") is meant to
  make unlikely.
- **Per-IP or per-connection limiting** -- out of scope; the actual
  threat model here (one shared token, no per-caller identity) doesn't
  support finer-grained limiting than process-wide without a bigger auth
  redesign, which this phase isn't undertaking.

## 25.9 Real-world bug found during Phase 20 testing

`test_doctor_reports_config_error_and_exits_2` passed in this sandbox
(no `.env` file exists here) but failed on real Windows hardware, a real
dev checkout with `.env` copied from `.env.example` per the README --
exactly the setup this whole phase is meant to help with. Real failure:
`assert result.exit_code == 2` got `SystemExit(1)` instead.

**Root cause:** `monkeypatch.delenv("LLM_MODEL", raising=False)` removes
`LLM_MODEL` from `os.environ` for the test, but `Config.load()` calls
`load_dotenv()`, which reads a real `.env` file on disk and refills any
variable not already set in the environment -- `load_dotenv()` doesn't
override an explicitly-set env var, but it does fill in one that's
merely absent, which is exactly the state `monkeypatch.delenv` leaves it
in. So on a machine with a real `.env`, `LLM_MODEL` came right back,
`Config.load()` succeeded, and the test actually exercised the Ollama
-unreachable path (exit 1) instead of the config-error path (exit 2) it
meant to test.

This is not a new failure mode -- it's a known, previously-fixed gotcha
in this exact test suite (`test_cli_cleanup.py::
test_cleanup_missing_llm_model_exits_2`,
`test_cli_session.py::test_start_missing_config_exits_2`, both already
guard against it), just missed when writing this phase's own test. The
fix used elsewhere applies unchanged: `mocker.patch("viva.config.
load_dotenv")` alongside the `monkeypatch.delenv` call, so nothing on
disk can refill what the test explicitly unset.

**Verified the fix for real, not just assumed:** reproduced by actually
creating a real `.env` file with `LLM_MODEL` set in this sandbox and
confirming the pre-fix test failed with the identical
`assert 1 == 2` / `SystemExit(1)` the Windows run reported, then
confirming the fix (matching the established pattern) passes with that
same `.env` file still present.

**Process note:** this class of bug -- a test that only passes because
the sandbox it was written in happens to lack a file a real dev
environment has -- is structurally invisible to any amount of care taken
*inside* this sandbox alone. It's exactly what the project's own
"real-world testing is the ground truth" principle
(`ways-of-working.md`) exists to catch, and did.
