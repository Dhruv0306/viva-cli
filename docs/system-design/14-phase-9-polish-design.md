# System Design Reference — Part 14: Phase 9 Polish Design

## 14.1 Scope

`docs/plan.md`'s Phase 9 entry lists four items: error handling for bad
URLs/model timeouts/huge repos, config validation, resume-session
support, and "repo/index cleanup and retention policy implemented and
tested (NFR7) — do not leave this implicit; it has no other phase
owner," plus a stretch web UI. An audit against Phases 0–8's actual
merged state (not just the plan's original wording) narrows this to one
real deliverable:

- **Config validation** — already complete. Every `.env.example` tunable
  Phase 1 through 8 introduced is validated in `Config.load()`
  (`config.py`), including the two fields its own docstring calls out
  as deliberately loose (`MAX_REDUCE_CONTEXT_TOKENS`, `GITHUB_TOKEN`).
  No gap found; no code change in this phase.
- **Resume-session support** — already complete, shipped in Phase 6
  (`viva resume`) and extended in Phase 7 (re-enqueuing unfinished
  evaluations on resume, `12-phase-7-evaluator-design.md` §12.6). Phase
  9 adds nothing new here.
- **Error handling for bad URLs / model timeouts** — already largely in
  place: `CloneError` (bad URL, auth failure) and `ConfigError` map to
  exit codes 2 across every command (`cli.py`), and `OllamaClient`
  already has a 120s default request timeout
  (`llm_client.py`). "Huge repos" is already FR3's file-cap +
  directory-stratified sampling, exercised in Phase 2. No new gap
  identified; flagged here for visibility rather than silently dropped,
  per this phase's own instruction not to leave NFR7 implicit — the
  same discipline applies to auditing the rest of the list, not just
  the item that turned out to need code.
- **`viva cleanup` (NFR7)** — genuinely unbuilt. This is Phase 9's real
  scope, and the rest of this doc covers only this.
- **Web UI** — stretch, explicitly out of scope per `plan.md` and
  reconfirmed out of scope by `13-phase-8-report-design.md` §13.10. Not
  attempted here.

This mirrors `05-repo-lifecycle-and-language-coverage.md` §5.4's retention
table and `06-cli-contract-and-profile-scaling.md`'s `viva cleanup` flag
spec — both already existed before this doc; this phase is where they get
implemented, not redesigned.

## 14.2 What `viva cleanup` actually touches

Three data categories, per §5.4's table, restated against what's really on
disk today:

| Data | Already handled? |
|---|---|
| Raw cloned repo source | Yes — deleted immediately once `INDEXING` completes (Phase 2/4). `viva cleanup` has nothing to do here. |
| SQLite `sessions`/`qa_records` rows + Project Profile JSON file | No removal path exists. `SessionStore` has no `delete_session`, and nothing removes the `profile_path` JSON file it points at. |
| Chroma collections | No removal path exists. `VectorStore` has `collection_exists`/`upsert_chunks`/`query`/`get_by_ids` but no delete. |

So the new work is: a `SessionStore.delete_session()`, a
`VectorStore.delete_collection()`, and something that decides *which*
sessions are in scope and drives both plus the profile-file removal.

## 14.3 The collection-reuse question (open question, now resolved)

`05-repo-lifecycle-and-language-coverage.md` §5.2 established that
Chroma collections are keyed `{repo_slug}-{commit_sha}` specifically so
they can be *reused* across sessions against the same unchanged commit.
That means a naive "delete everything this session touched" cleanup is
wrong: two sessions can share one `collection_name`, and deleting it
because session A aged out would silently break session B's still-valid
`viva report B` (its `qa_records.grounding_chunk_ids` would resolve to
nothing).

**Decision: reference-count collections against the `sessions` table
itself, not a new bookkeeping structure.**

A `collection_name` is deleted from Chroma only once *no remaining
session* (i.e. no session that cleanup is *not* removing) still points
at it. Concretely:

1. Partition all sessions into `targets` (aging out, or all of them if
   `--all`) and `survivors` (everyone else).
2. Collect `survivor_collections = {s.collection_name for s in survivors}`.
3. For each `target`, delete its SQLite rows and profile file
   unconditionally (those are never shared across sessions).
4. For each *distinct* `collection_name` touched by `targets`, delete it
   from Chroma only if it's not in `survivor_collections`.

This falls directly out of data `sessions` already has — no new
reference-count column, no second table to keep in sync. Same "one
source of truth" bias the project already applies elsewhere (`qa_records`
uses implicit rowid ordering instead of a dedicated sequence column,
per `03-final-architecture.md`).

**Age is judged by `updated_at`, not `created_at`.** `viva resume`
bumps `updated_at` (`update_status`, called on every state transition).
A session someone actively resumed six weeks after starting it should
not be swept just because it was first created outside the retention
window — `updated_at` reflects "was this session recently touched,"
which is what retention is actually trying to measure. `created_at`
would make an old-but-actively-resumed session indistinguishable from
one that's genuinely abandoned.

This resolves the open design question flagged when Phase 9 was
scoped: no separate reference-counting mechanism needed, and
`updated_at` (not `created_at`) is the retention clock.

## 14.4 Module boundary: `viva/cleanup.py`

Following the same shape `ReportBuilder` established in Phase 8 — a
plain, dependency-injected class/function over data the CLI command
already has, not something wired into `Orchestrator` — cleanup logic
lives in a new `src/viva/cleanup.py`, not inside `cli.py` or
`orchestrator.py`. Rationale, mirroring `13-phase-8-report-design.md`
§13.2's for `ReportBuilder`: nothing about cleanup is session-time or
state-machine-relevant. It's an out-of-band maintenance operation the
CLI command drives directly against `SessionStore` and `VectorStore`,
same as `list_sessions` already reads `SessionStore` directly without
routing through the Orchestrator.

```python
@dataclass(frozen=True)
class CleanupReport:
    sessions_removed: list[str]
    collections_removed: list[str]
    profiles_removed: list[str]
    sessions_retained: int

def run_cleanup(
    store: SessionStore,
    vector_store: VectorStore,
    older_than_days: int,
    purge_all: bool = False,
) -> CleanupReport: ...
```

`run_cleanup` takes real `SessionStore`/`VectorStore` instances (not
paths) — same dependency-injection pattern `ReportBuilder.build()` and
`Orchestrator.__init__` already use, so tests exercise the real logic
against `tmp_path`-backed stores without mocking, consistent with
`test_indexer_store.py`'s "test real behavior where it's cheap" note
and `test_report.py`'s pure-dataclass-fixture approach.

A missing `profile_path` file (already deleted, or never written
because the session failed before `INDEXING`) is not an error —
`os.path.exists()` is checked before `os.remove()`, same
degrade-gracefully posture `VectorStore.get_by_ids` already applies to
missing chunk IDs.

## 14.5 New config

None. `SESSION_RETENTION_DAYS` already exists (`config.py`,
`session_retention_days`, default `7`) — Phase 1 defined it ahead of any
consumer, per FR28's "environment-file configurable, not hardcoded" and
`04-open-questions.md` item 4's resolution. `viva cleanup` is simply its
first real reader. `--older-than` overrides it per-invocation only, per
the CLI contract; no new env var.

## 14.6 `viva cleanup` CLI command

Follows `06-cli-contract-and-profile-scaling.md` §6.1's existing spec
exactly — this doc doesn't change that contract, only implements it:

```
viva cleanup [--older-than <days>] [--all]
```

- `--older-than <days>` (default: `SESSION_RETENTION_DAYS`) — sessions
  with `updated_at` older than `now - days` are removed, per §14.3.
- `--all` — ignores age entirely; every session is a target. Collection
  reference-counting (§14.3) still applies mechanically, but with no
  survivors, every touched collection is deleted too — a true full
  reset falls out of the same algorithm rather than needing a separate
  code path.
- Exit codes: `0` on success (including "nothing to remove" — an empty
  retention sweep is not an error), `2` for bad config or a
  non-positive `--older-than`, `1` for an unexpected failure mid-sweep.
  No `3` case — `viva cleanup` takes no `session_id` argument, so the
  "valid input, not actionable" exit code that `resume`/`report` use
  for a missing/wrong-state session doesn't apply here.

## 14.7 Migration / blast radius

- `src/viva/storage/session_store.py` — new `delete_session(session_id)`:
  deletes `qa_records` rows for the session, then the `sessions` row.
  No FK/CASCADE in the schema (`schema.py`'s existing plain-TEXT-column
  choice), so order matters and is explicit rather than relying on the
  database to enforce it.
- `src/viva/indexer/store.py` — new `delete_collection(name)`: no-op if
  the collection doesn't already exist, mirroring `collection_exists`'s
  existing check-before-act pattern in the same class.
- `src/viva/cleanup.py` — new: `CleanupReport`, `run_cleanup` (§14.4).
- `src/viva/cli.py` — new `cleanup` command (§14.6).
- `README.md` — `viva cleanup` usage example, "Phases 0-9" status line.
- New tests: `tests/test_session_store.py` gets `delete_session` cases;
  `tests/test_indexer_store.py` gets `delete_collection` cases;
  `tests/test_cleanup.py` (aggregation logic, real tmp-path-backed
  stores, no mocking — §14.4); `tests/test_cli_cleanup.py` (CLI
  contract: exit codes, `--older-than`, `--all`, and the
  collection-survives-when-shared case from §14.3).

## 14.8 Explicitly out of scope

- Web UI (stretch, per `plan.md`; reconfirmed out of scope by
  `13-phase-8-report-design.md` §13.10).
- Any change to the retention *window* default (`SESSION_RETENTION_DAYS`
  stays `7`, per `04-open-questions.md` item 4 — already resolved, not
  reopened here).
- Automatic/scheduled cleanup (e.g. running the sweep on every `viva
  start`). `viva cleanup` is on-demand only, per the CLI contract as
  already written — an implicit background sweep would be a new design
  decision (surprising side effects on an unrelated command) this doc
  doesn't make.

## 14.9 Caveat on this doc's provenance

Unlike Phases 0–8's design docs, the design decisions in §14.3 (survivor-
based reference counting, `updated_at` as the retention clock) are new
ground — the prior docs established *that* retention needed a policy
and roughly what `viva cleanup`'s flags should be, but never actually
worked through the shared-collection interaction, because no phase
before this one needed to. Flagging this explicitly per the project's
own "flag unverified claims rather than assert false certainty"
convention: these two decisions are this doc's proposal, not a
recorded prior resolution, and are worth a second look before/while
reviewing the patch series below.
