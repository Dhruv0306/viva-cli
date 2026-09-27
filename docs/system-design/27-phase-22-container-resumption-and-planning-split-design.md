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
