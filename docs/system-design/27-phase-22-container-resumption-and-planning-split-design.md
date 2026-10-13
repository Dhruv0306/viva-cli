# System Design Reference — Part 27: Phase 22 Container Resumption &
Planning-Module Split Design

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. Implementation design for `docs/plan.md`
> Phase 22. Written against `main` at commit `08163974` (Phase 21 shelved,
> v0.4.0 released).

Two unrelated items, bundled the same way Phase 14 and Phase 18 each
bundled multiple independent small items into one phase. Part A has no
open design questions — it's a resumption of already-decided work. Part
B is the one this doc actually needs to settle before code.

## 27.1 Part A — Resuming Phase 21 (containerized setup)

Nothing here is redesigned. `26-phase-21-containerized-setup-design.md`
§26.1–§26.7's decisions stand as written, and §26.11/§26.12's two
real-world fixes (the root-then-drop-privileges `entrypoint.sh`, the
`.gitattributes`/`sed` CRLF fix) are already known-good — this is
restoring what §26.13 removed, not rebuilding it.

- Restore `Dockerfile`, `docker-compose.yml`, `.dockerignore`,
  `entrypoint.sh`, `.gitattributes`, and the README's Docker section from
  the pre-shelving commit (`git log` on those paths finds it directly —
  no need to hand-reconstruct from the design doc's code blocks).
- **Exit criteria** are exactly the three §26.9 items that were still open
  at shelving time (§26.13): a full session (start → answer → report)
  completing entirely through the containerized path against a real repo
  and a real external Ollama instance; `docker compose down && up`
  confirmed to preserve session data via the `data/` bind mount, not just
  a plain restart; a language not yet in the `tree_sitter_cache` named
  volume downloading its grammar at runtime, then a second run after a
  container restart confirmed *not* re-downloading it.
- Everything §26.9 already confirmed before shelving (build succeeds,
  container starts and binds correctly, browser reachability, token auth
  end-to-end, session creation) is trusted as still valid rather than
  re-verified from scratch — it's the same artifacts being restored
  unmodified, not new code with a reason to doubt it. If any of it fails
  on re-verification, that's a real regression worth its own `§27.x`
  finding, not silently assumed away.
- **This part's `viva serve --host 0.0.0.0` requirement (§26.3) is
  intentional and stays** — Docker's port mapping needs it, and it
  correctly triggers Phase 15's existing token requirement with no new
  code. This is unrelated to Part B's testing convention below, which is
  about ad hoc `viva serve` runs outside Docker, not the container's own
  contract.

## 27.2 Part B — root cause

`orchestrator.py` is 806 lines. Three genuinely separable concerns are
interleaved in one class today:

1. **Pipeline orchestration** — `start()`/`resume()`/`_run_setup_pipeline()`/
   `_run_planning()`/`_run_live_session()`: sequencing Ingest → Analyzer →
   Indexer → QuestionGen, persisting state transitions through
   `SessionStore`, driving the timer. This is the Orchestrator's actual
   job per `design.md` §1/§2 and stays exactly where it is.
2. **Planning/ranking** — `_rank_pending_items()`, `_select_next_item()`,
   `_replenish_plan()`, plus the already-asked-target/category bookkeeping
   they depend on. Pure ordering/sizing decisions over `QuestionPlanItem`/
   `QARecordRow` data (FR15, design.md §7, Phase 13's phase-ordering
   addition, §18.4). This is §19.2.2's target.
3. **Semantic-duplicate detection** — `_cosine_similarity()`,
   `_is_semantic_duplicate()`, `_seed_embedding_cache()`. Deliberately
   **out of scope** for this split: it's a different concern from ranking
   (embedding-based similarity, not target/category ordering) and
   `_rank_pending_items()` already doesn't touch it — `_run_live_session`
   calls both independently. Splitting it too would widen this phase past
   what §19.2.2 actually asked for, with no functional payoff to justify
   it. Revisit separately if/when embedding-based dedup logic grows.

## 27.3 New module: `viva/planning.py`

Matches the project's existing convention for this shape of module —
thin, mostly-stateless, plain dataclasses/lists in and out (same pattern
as `questiongen/retrieval.py`, `cleanup.py`) — rather than introducing a
stateful `Planner` class with no precedent elsewhere in this codebase.

```python
# viva/planning.py
"""Pure question-ordering and plan-sizing logic, split out of
Orchestrator (docs/plan.md Phase 22, docs/system-design/27-...-design.md
§27.2's finding 19.2.2). No SessionStore access here -- every function
takes the data it needs as arguments and returns a value; Orchestrator
still owns every store read/write and every side effect.
"""

def rank_pending_items(
    pending: list[QARecordRow],
    already_asked_targets: set[str],
    already_asked_categories: set[str],
) -> list[QARecordRow]:
    """Moved verbatim from Orchestrator._rank_pending_items -- same
    phase/duplicate-target/repeat-category sort key, same "prefer, never
    exclude" contract (docs/system-design/11-phase-6-session-loop-design
    .md §11.9). Only change: already_asked_targets/categories are passed
    in instead of queried from self.store, so this has no I/O."""
    ...  # _phase()/_priority() move here unchanged as module-level helpers


def compute_replenished_plan(
    profile: ProjectProfile,
    config: Config,
    current_top_level_count: int,
) -> list[QuestionPlanItem] | None:
    """Moved from Orchestrator._replenish_plan's pure part: build a
    higher-ceiling plan and decide whether it actually grew. Returns the
    new plan if len(new_plan) > current_top_level_count, else None --
    Orchestrator's wrapper below turns that into the same True/False
    contract _replenish_plan already has, and still owns save_plan()
    (the actual write) and the get_qa_records() count itself."""
    replenished_config = dataclasses.replace(
        config, max_questions=current_top_level_count + _REPLENISH_INCREMENT
    )
    new_plan = build_coverage_plan(profile, replenished_config)
    if len(new_plan) <= current_top_level_count:
        return None
    return new_plan
```

`_REPLENISH_INCREMENT` moves to `planning.py` with the function that uses
it. `_MAX_DEDUP_CANDIDATES` stays in `orchestrator.py` — it's consumed by
`_run_live_session`'s dedup-candidate loop (finding 3 above, out of
scope), not by anything moving.

## 27.4 What `orchestrator.py` looks like after the split

Every existing method name and signature Orchestrator exposes today is
**kept**, as thin delegates — this is the mechanism that makes "existing
test suite passing unchanged" a valid regression check for a behavior-
preserving refactor (`tests/test_orchestrator.py` calls `orch
._select_next_item(...)` and `orch._replenish_plan(...)` directly, and
monkeypatches `_replenish_plan` in three places — confirmed by grep
against the real test file, not assumed):

```python
def _rank_pending_items(self, session_id, pending):
    already_targets = self._already_asked_targets(session_id)
    already_categories = self._already_asked_categories(session_id)
    return planning.rank_pending_items(pending, already_targets, already_categories)

def _replenish_plan(self, session_id, profile):
    current_top_level = sum(
        1 for r in self.store.get_qa_records(session_id) if r.is_followup_of is None
    )
    new_plan = planning.compute_replenished_plan(profile, self.config, current_top_level)
    if new_plan is None:
        return False
    self.store.save_plan(session_id, new_plan)
    return True
```

`_select_next_item()` is untouched — it already only calls
`self._rank_pending_items(...)`, which still exists. `_already_asked_targets`/
`_already_asked_categories`/`_already_asked_question_texts`/
`_already_asked_count` stay on `Orchestrator` — they're `self.store` reads,
not planning logic, and `rank_pending_items()` takes their *output* as
plain `set[str]` arguments rather than reaching for the store itself.

Net effect: `orchestrator.py` loses roughly 90-100 lines (the two
functions' bodies plus their docstrings) to a new ~60-line
`planning.py`; every call site elsewhere in the codebase (`cli.py`,
`web/`) is untouched, since none of them call these methods directly.

## 27.5 Testing

- `tests/test_planning.py` (new): the ranking/replenishment test cases
  currently exercised indirectly through `orch._select_next_item(...)`/
  `orch._replenish_plan(...)` in `test_orchestrator.py` get direct
  pure-function coverage against `planning.rank_pending_items()`/
  `planning.compute_replenished_plan()` — plain dataclass fixtures, no
  `SessionStore`/mocking needed, matching this project's existing "pure-
  dataclass fixtures for unit tests" convention.
- `tests/test_orchestrator.py`'s existing `_select_next_item`/
  `_replenish_plan` tests are **not deleted** — they now exercise the
  thin delegates plus the real store I/O around them (already-asked-set
  computation, `save_plan()` actually persisting), which is exactly the
  integration layer `planning.py`'s own pure tests can't cover.
- No behavior change means no pre-fix-failing regression test the usual
  way — the full existing suite passing unchanged, before and after, at
  every commit is the check.
- Per your instruction: any ad hoc `viva serve` run during this phase to
  confirm the split hasn't broken the web path stays on the CLI default
  (no `--host` flag, loopback, no token) — separate from Part A's
  container work, which correctly needs `--host 0.0.0.0` inside Docker.

## 27.6 Exit criteria

- **Part A:** the three items in §27.1 confirmed on real Docker, on real
  Windows hardware, the same standard every other phase in this series
  has held to.
- **Part B:** full test suite (currently 651 tests) passes unchanged;
  `ruff`/`mypy --strict` clean against the new `planning.py`; a plain
  `viva start` end-to-end run and a plain `viva serve` (no `--host`)
  session both confirmed live, showing identical question ordering/
  replenishment behavior to a pre-split run against the same repo —
  behavior-preserving confirmed in practice, not just by the diff being
  small.

## 27.7 Real-world finding: token still required after Part A validation

Confirmed working on real Docker (§27.1's exit criteria), but Dhruv's
first real run surfaced a follow-up question this doc hadn't spelled
out: why does `docker compose up` still print an access token, when
loopback-only `viva serve` normally doesn't?

**Root cause, confirmed directly against `web/app.py`/`cli.py`, not
assumed:** the token requirement (§21.3/`_requires_auth()`) is an
explicit allowlist keyed on the literal `--host` string `viva serve`
was started with (`{"127.0.0.1", "localhost", "::1"}` = no token,
anything else = token) -- deliberately "fail-open-to-requiring-auth"
per its own comment, not a live reachability check. The Dockerfile's
`CMD` passes `--host 0.0.0.0`, and that's not optional: a process bound
only to `127.0.0.1` *inside* a container listens on that container's
own network namespace's loopback, which Docker's port-mapping/NAT
reaches via the container's external interface, not its loopback -- so
binding `127.0.0.1` inside the container would make `viva serve`
completely unreachable from the host, not just tokenless. This is a
hard constraint of how Docker networking works, not a viva-cli
decision, and isn't something a code change here can route around
without breaking the container's actual reachability.

**What Part A ships instead:** `docker-compose.yml`'s `ports` mapping
changed from `"8000:8000"` to `"127.0.0.1:8000:8000"` -- this is a
host-side restriction (only your own machine can reach the published
port at all, not other machines on your network), which is the real
security property `_requires_auth()`'s non-loopback check was written
to protect (§19.4.1: "that's no longer just this machine talking to
itself"). It does not remove the token, because `_requires_auth()` has
no way to see that host-side restriction -- it only ever sees the
`--host` string passed inside the container, which is still `0.0.0.0`
either way.

**Not done, and not done silently:** actually removing the token for
this case would mean either (a) trusting the container's `0.0.0.0`
bind as "safe" whenever some other signal suggests loopback-only
publishing -- but nothing inside the container's network namespace can
see how `docker-compose.yml` published its own port, so this isn't
implementable without inventing a new signal (e.g. an env var the
person sets themselves, which is really just moving the trust decision
onto the person, not eliminating it) -- or (b) weakening
`_requires_auth()`'s check itself. Both are real changes to a
deliberately-designed security boundary (§19.4.1, §21.3), not a bug
fix, and this doc's own "agree before code" convention means that's a
decision for Dhruv to make explicitly, not something to default into
while restoring already-decided Phase 21 work.

## 27.8 Real-world finding: tree-sitter query files silently dropped from the installed wheel

Surfaced by the same real Docker run as §27.7, but a different and more
serious bug: `viva.analyzer.extract`'s AST extraction failed for
`calculator.java` with `FileNotFoundError: .../viva/analyzer/queries/
java.scm`, falling back to the line-window path (`extract.py`'s own
try/except catches this and degrades gracefully -- no crash, no
visible error to the person, just a silently worse question for that
file).

**Root cause:** the exact same bug §26.9's `viva.web` static-file fix
already exists as documented history for (`pyproject.toml`'s
`[tool.setuptools.package-data]`, its own comment explaining the same
mechanism) -- setuptools drops any non-`.py` file from a built wheel
unless explicitly listed in `package-data`, and `src/viva/analyzer/
queries/*.scm` was never added when that section was first written.
Every dev/CI install so far has used `pip install -e .` (editable),
which reads straight from the source tree and never exercises this
path -- so this was invisible until the Dockerfile's `pip install
--no-cache-dir .` (a real, non-editable install) actually ran, same as
how the static-file bug was originally found. Confirmed by building a
clean venv and installing non-editably: only `viva.web`'s `static/*`
existed as a package-data rule; a fresh install's `queries/` directory
was checked and found to contain all nine `.scm` files as loose
uninstalled source, not present in `site-packages` at all -- not just
`java.scm`, every language.

**Fix:** added `"viva.analyzer" = ["queries/*.scm"]` alongside the
existing `viva.web` rule. Verified the same way the original bug was
verified -- a clean venv, `pip install --no-cache-dir .` (non-editable,
matching the Dockerfile exactly), then confirming all 11 `.scm` files
present in the installed `site-packages` tree. Full test suite (665)
still passes unchanged -- this is a packaging manifest fix, no source
code touched.

## 27.9 Not a viva-cli bug: "Failed to connect to Ollama" mid-session

The screenshot from the same run shows a session stuck at "Analyzing
codebase..." with `Failed to connect to Ollama. Please check that
Ollama is downloaded, running and accessible.` -- this exact string is
the `ollama` Python client's own hardcoded `CONNECTION_ERROR_MESSAGE`
(confirmed by reading the installed package directly, not guessed),
raised whenever it can't open a connection to the configured
`OLLAMA_HOST` at all. It is not a viva-cli code path -- nothing in this
project constructs that string.

**Most likely cause, not yet confirmed:** Ollama on Windows/most
platforms binds to `127.0.0.1:11434` on the *host* by default. Docker
Desktop's `host.docker.internal` correctly resolves to the host's IP
from inside the container (that part of `docker-compose.yml`'s
`extra_hosts`/default `OLLAMA_HOST` is working -- the container
reached the host machine), but if Ollama itself is only listening on
the host's loopback interface, the host's own network stack refuses a
connection arriving from the container's bridge network (a different
source address than `127.0.0.1`) even though the hostname resolved
fine. This is a common Docker+Ollama interaction, not something
`docker-compose.yml`'s networking config can fix from the container
side -- it needs Ollama itself, on the host, told to listen more
broadly (Windows: `setx OLLAMA_HOST "0.0.0.0"` then restart the Ollama
service/app; confirm with `netstat -ano | findstr 11434` showing
`0.0.0.0:11434` rather than `127.0.0.1:11434`).

**Not fixed here** -- there's nothing in this repository to fix if
that's the cause; worth confirming before treating it as anything
else. If Ollama was, in fact, listening on `0.0.0.0` and this still
failed, that would point at something in `docker-compose.yml` instead
and is worth a fresh report with `docker compose logs` alongside
`netstat` output from the host at the time it failed.

## 27.10 Correction to §27.9: the real cause was `.env` interpolation

Dhruv's next run disproved §27.9's "most likely cause". `netstat` showed
Ollama already listening on `0.0.0.0:11434` and `[::]:11434` before any
change, and the session still failed with the same client error, so the
host-side bind address was a red herring. (A first `netstat` in that
transcript printed nothing at all, meaning Ollama was not running at that
moment, so some failures in that sequence may also just be Ollama being
down; the cause below is independent of that and reproduces whenever
Ollama is up.)

**Root cause, found by reading the restored `docker-compose.yml` and
`.env.example` together:** the compose file passed
`OLLAMA_HOST=${OLLAMA_HOST:-http://host.docker.internal:11434}`. The
`:-` default only applies when `OLLAMA_HOST` is *unset*, but `docker
compose` reads the project's `.env` for `${...}` substitution, and
`.env.example` ships `OLLAMA_HOST=http://localhost:11434` for the native
install. Anyone who copied it to `.env` (required anyway, since
`LLM_MODEL=${LLM_MODEL}` has no default) handed the container
`localhost`, which inside a container is the container itself. The
supporting evidence: the container started without a "LLM_MODEL is not
set" error, so compose demonstrably was reading `.env`. The README's own
advice ("set `OLLAMA_HOST` in `.env`") pointed the same wrong way.

**Why this was missed:** §26.9's earlier validation ran without a copied
`.env` carrying `OLLAMA_HOST`, so the default always won. It only bites
the exact setup the README tells people to use.

**Fix:** compose now reads a Docker-specific name,
`${DOCKER_OLLAMA_HOST:-http://host.docker.internal:11434}`, so the
native `OLLAMA_HOST` can never leak into the container. This also
protects against a shell-level `OLLAMA_HOST=0.0.0.0` (the value Ollama's
own docs recommend setting on the host), which would otherwise have been
substituted as the client URL. `.env.example` and the README now say so.
Not verified in this authoring environment (no Docker); confirm with
`docker compose config`, which prints the resolved value.

## 27.11 Validation on real Docker (Windows), 2026-10-12

**Confirmed, with the evidence:**

- §27.8 fix: a full Java session logged no `java.scm` warning (the
  earlier run's log had the traceback).
- §27.10 fix: with a freshly copied `.env.example` (which sets
  `OLLAMA_HOST=http://localhost:11434`, the exact condition that caused
  the bug), `docker compose config` resolved the container's
  `OLLAMA_HOST` to `http://host.docker.internal:11434`, and sessions
  reached Ollama and completed.
- Exit criterion 1: two full sessions through the containerized path on
  real Ollama, each with HTML report view and token-authenticated
  Markdown download: `Calculator_Java` (4 of 4 answered) and
  `viva-cli-test-c` (2 answered, status COMPLETE; 5 planned questions
  not reached because the 2-minute session ended, which the report
  states).
- Exit criterion 2: after the container was recreated, the Calculator
  session was still listed and its report still served (200), so the
  `data/` bind mount persists. The restart's output also listed no
  "Volume ... Created" line for `tree_sitter_cache`, so that named
  volume survived too.
- Part B in a real run: both sessions finished with more questions
  planned than the initial ceiling (2 to 4, and 1 to 7; 7 is
  1 + `_REPLENISH_INCREMENT` of 6), consistent with
  `compute_replenished_plan()` having run. Follow-ups could also add
  questions, so this is consistent with, not proof of, replenishment.
  No pre-split comparison run exists. The CLI `viva start` path was not
  exercised.

**Correction to §26.1 and the old README wording:** I checked
`tree-sitter-language-pack` 1.21.3 (the version the image installed) in
the authoring environment with an empty `HOME`. It does not download per
language. The first use of any language silently downloads one ~22 MB
bundle into `~/.cache/tree-sitter-language-pack/v1.21.3/bundles/`, then
extracts that language's library into `.../libs/` on demand; other
languages later extract from the cached bundle with no download. Two
consequences: nothing is logged, so container logs can never show a
download or its absence; and running Java, restarting, then running C
(what the first validation did) does not isolate persistence. The README
now says this.

**Grammar cache persistence, confirmed by listing the volume instead of
reading logs:**

    docker run --rm --entrypoint ls -v viva-cli_tree_sitter_cache:/cache viva-cli-viva:latest -laR /cache

The listing showed one bundle (`bundles/linux-x86_64-...tar.zst`,
22729499 bytes) and `libs/libtree_sitter_java.so` both stamped 06:35
UTC, the minute the Java session started; and
`libs/libtree_sitter_c.so` stamped 07:02 UTC, when the C session ran
after the container was recreated at about 06:54 UTC. So the bundle and
the Java library survived the restart, and C was extracted from the
cached bundle with no second download (still a single bundle file, still
06:35). The mount path matches where the package caches. This closes
Part A's third exit criterion.

**Unrelated observations, not Phase 22 work:** the Calculator repo
vendors a `JRE/` directory, so ingest took about 7 minutes before
planning and all four questions were about `JRE/lib/security/
default.policy` rather than the calculator code; and several entries in
that report's Weaknesses list read as plain facts ("The grant includes
...") rather than critiques. Both are candidates for the backlog
(vendored-directory filtering, evaluator output quality).

## 27.12 Real-world finding: `OLLAMA_HOST=0.0.0.0` breaks the native CLI on Windows

The Part B `viva start` run failed twice at "Analyzing codebase..." with
the `ollama` client's `Failed to connect to Ollama` message, even though
`ollama list` worked and Docker sessions had been working.

**Root cause, confirmed by the workaround:** an earlier `setx
OLLAMA_HOST "0.0.0.0"` (made while chasing §27.9) left that value in new
terminals (`echo $env:OLLAMA_HOST` printed `0.0.0.0`). `OLLAMA_HOST` is
overloaded: for the Ollama server it is a bind address, but
`config.py` also reads it as the client URL, and a real environment
variable beats `.env` (`load_dotenv` does not override). The `ollama`
Python client turns `0.0.0.0` into `http://0.0.0.0:11434`, which Linux
treats as localhost (so the container never hit this) but Windows cannot
connect to. Setting `$env:OLLAMA_HOST = "localhost"` in that terminal
made the same command run to COMPLETE.

**Fix:** `config.py` now passes `OLLAMA_HOST` through
`_normalize_ollama_host()`, which rewrites a bind-all host (`0.0.0.0`,
`[::]`, or a bare `::`) to `localhost`, keeping any scheme, port and path
(`http://0.0.0.0:11434` becomes `http://localhost:11434`). Everything
else is left exactly as given, including `localhost`, `127.0.0.1`, LAN
addresses and `host.docker.internal`, so the Docker path is unaffected.
The rewrite lives where the value is read, so every consumer (CLI
commands, orchestrator, `viva doctor`, web server) is covered, and it
adds no `Config` field, so no test fixture elsewhere needed changing.
Ollama's own server setting is untouched.

**Tests:** 15 new parameterized cases in `tests/test_config.py`: 8
bind-all forms that must become `localhost`, and 7 values that must pass
through unchanged, including `http://0.0.0.0.example.com:11434` (a
hostname that merely starts with `0.0.0.0`). The 8 rewrite cases were
confirmed failing against the old code before the fix. Full suite: 680
passed, 1 skipped; `ruff` and `mypy --strict` clean.

**Confirmed on real hardware (2026-10-13):** with `$env:OLLAMA_HOST =
"0.0.0.0"` set and no other workaround, `viva doctor` printed `Ollama
reachable at localhost` and found `LLM_MODEL`. `viva start` was not
re-run with `0.0.0.0`; it reads the same `Config` value, which `doctor`
shows is now `localhost`.

## 27.13 Real-world finding: `viva doctor` falsely reports a bare model name as not pulled

The same `viva doctor` run printed `EMBEDDING_MODEL 'nomic-embed-text' is
not pulled` and told the user to run `ollama pull nomic-embed-text`. The
user pulled it (it was already there, `ollama list` showed
`nomic-embed-text:latest` all along) and the report did not change, so
re-pulling can never clear it. The model was fine: earlier Docker and CLI
sessions had embedded with exactly that name.

**Root cause:** `doctor()` (Phase 20, §25.3) built the set of pulled
names from `ollama.Client.list()` and tested `model in pulled` as an
exact string match. Ollama always lists tagged names
(`nomic-embed-text:latest`) and treats a bare name as `:latest`, but the
shipped default `EMBEDDING_MODEL` is the bare `nomic-embed-text`. So the
default configuration failed `doctor` on any machine. `LLM_MODEL`
passed only because the example value carries an explicit tag
(`gemma4:e4b`).

**Why it was missed:** `test_doctor_all_checks_pass_exits_0` faked the
list response with the bare name `"nomic-embed-text"`, which real Ollama
never returns, so the test encoded the same wrong assumption as the code
and passed. This is the "fixture that matches the bug" pattern, and only a
real run on real Ollama could expose it.

**Fix:** a small `_with_default_tag()` helper in `cli.py` appends
`:latest` to any name with no tag, applied to both the pulled names and
the configured name before comparing. Only the last path segment is
checked for a tag, so `someone/embedder` is normalized and a registry
host with a port is not mistaken for tagged. A bare name still does not
match a different tag (`nomic-embed-text:v1.5` does not satisfy
`nomic-embed-text`). The message still echoes the name as configured.
Embedding and LLM calls were never affected; only the `doctor` report.

**Tests:** three new cases in `tests/test_cli_doctor.py` (bare name
matches `:latest`, bare namespaced name matches `:latest`, bare name does
not match a different tag). The first two were confirmed failing against
the old code. Full suite: 683 passed, 1 skipped; `ruff` and `mypy
--strict` clean. The old all-pass test is kept as is; it still passes
because both sides are now normalized.

**Confirmed on real hardware (2026-10-13):** after the fix, `viva doctor`
printed all four checks green, including `EMBEDDING_MODEL
'nomic-embed-text' is pulled`.

