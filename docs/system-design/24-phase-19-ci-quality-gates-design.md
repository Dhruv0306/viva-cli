# System Design Reference — Part 24: Phase 19 CI Quality Gates Design

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. Implementation design for `docs/plan.md`
> Phase 19. Written against `main` at commit `1878f28` (the commit that
> landed Phase 18's corrected patch 1). Every number in this doc came from
> actually running `ruff`, `mypy --strict`, and `pytest --cov` against
> this codebase, not from estimating what a fresh adoption would probably
> find — after Phase 18's `httpx2` mistake, "probably" isn't good enough
> for a finding that's about to become a merge-blocking CI gate.

## 24.1 `ruff`

### 24.1.1 What a naive adoption would flag

`ruff check src/ tests/ --select=E,F,W,B,UP` (default rules plus
`flake8-bugbear` and `pyupgrade`) on the current tree:

```
696  E501   line-too-long
 32  B904   raise-without-from-inside-except
  8  UP037  quoted-annotation
  5  UP017  datetime-timezone-utc
  5  F401   unused-import
  3  B027   empty-method-without-abstract-decorator
  3  UP045  non-pep604-annotation-optional
  3  B905   zip-without-explicit-strict
  2  F841   unused-variable
  1  UP035  deprecated-import
  1  B008   function-call-in-default-argument
  1  UP007  non-pep604-annotation-union
Found 760 errors. [*] 23 fixable with --fix (5 more with --unsafe-fixes)
```

696 of 760 (92%) is `E501` alone, at ruff's default 88-column limit. This
codebase currently has 720 lines over 88 columns, 241 over 100, and 41
over 120 (measured directly, not estimated). Gating on `E501` today means
either a large mechanical reformat landing as part of "add CI gates" —
exactly the kind of large, review-heavy diff bundled into an unrelated
change that this project's own conventions steer away from — or picking
an arbitrary wider limit to make the number look smaller without actually
deciding anything.

### 24.1.2 Decision: `E501` is excluded from this phase, deliberately

Not an oversight, and not silently dropped — recorded here as a real
decision with a real reason. Line-length enforcement is a formatting
choice most naturally paired with adopting `ruff format` (or `black`)
itself, which is a separate tooling decision from "catch real bugs and
dead code before merge" and deserves its own sign-off rather than riding
in on this phase's back. `pyproject.toml`'s `[tool.ruff]` section gets
`select = ["E", "F", "W", "B", "UP"]` with `ignore = ["E501"]`, and a
comment saying exactly this, so a future reader doesn't mistake the
absence for an accident.

### 24.1.3 The remaining 64 findings are small enough to actually fix now

Unlike `E501`, these are worth fixing as part of this phase rather than
deferred, since each is either mechanical or a one-line, individually
-judged fix:

- **`B904` (32, all in `src/viva/cli.py`):** every instance is the same
  pattern — catch an exception, `console.print` a clean Rich-formatted
  user-facing message, then `raise typer.Exit(code=N)`. Deliberate CLI UX
  (a bare Python traceback would defeat the point of the formatted
  message), not a bug, but bugbear can't tell the difference from an
  accidentally-swallowed exception without `from None` making the intent
  explicit. Fix: `raise typer.Exit(code=N) from None` at all 32 sites —
  `from None`, not `from exc`, since chaining the original traceback is
  exactly what this pattern intentionally avoids.
- **`F401`/`F841` (5 + 2, unused imports/variables):** `--fix` handles
  these; a one-by-one read after, since an unused import occasionally
  means an incomplete removal elsewhere worth double-checking, not just
  an unused import.
- **`UP*` (18 total: quoted annotations, `Optional`/`Union` → `|`,
  `datetime.timezone.utc` → `datetime.UTC`, deprecated `typing` imports):**
  all auto-fixable with `--fix`, no manual judgment needed.
- **`B905` (3, `zip` without `strict=`):** genuine correctness nuance —
  `zip()` silently truncates on length mismatch; `strict=True` turns a
  silent data-alignment bug into a loud `ValueError`. Each of the 3 sites
  needs a quick look to confirm the sequences are always expected to be
  equal length (if so, `strict=True`; if intentionally unequal, an inline
  `# noqa: B905` with a one-line reason).
- **`B027` (3, empty method without `@abstractmethod`):** likely
  intentional no-op hooks on a base class — confirm each is meant to be
  silently overridable (keep as-is with `# noqa: B027` and a reason) vs.
  actually meant to force a subclass override (add the decorator).
- **`B008` (1, `src/viva/cli.py:626`, `typer.Option(...)` as an argument
  default):** false positive — this is Typer's own idiomatic API
  (`typer.Argument(...)`/`typer.Option(...)` as parameter defaults is how
  every Typer command in this file is written, not a one-off). The same
  false-positive class would also fire on FastAPI's `Depends(...)`
  pattern if `web/app.py` ever adopts it — it doesn't today, checked
  directly (`grep -n "Depends(" src/viva/web/app.py` — no hits), so only
  the Typer case is live, but both are worth knowing about since this
  project uses both frameworks. Fix: add `B008` to
  `[tool.ruff.lint.per-file-ignores]` for `src/viva/cli.py`, with a
  comment naming the Typer pattern explicitly, rather than a bare inline
  `# noqa` easy to lose track of across 20+ command functions.

## 24.2 `mypy`

### 24.2.1 What `--strict` actually finds

`mypy --strict src/viva`, installed via `pip install -e ".[dev]"` (no
voice extra):

```
85 errors in 17 files (checked 47 source files)
```

By error code: 27 `arg-type`, 25 `type-arg`, 11 `no-untyped-def`,
9 `union-attr`, 4 `no-untyped-call`, 3 `import-not-found`, plus a handful
of singletons. By file, concentrated rather than spread evenly:
`evaluator.py` (14), `web/app.py` (12), `voice_io.py` (12),
`indexer/store.py` (11), `web/web_session_ui.py` (6), `session_ui.py` (5),
`orchestrator.py` (5), `analyzer/extract.py` (5), the remaining 9 files
with 1-4 each.

### 24.2.2 The 3 `import-not-found` errors are a config gap, not real errors

All 3 are in `voice_io.py`: `faster_whisper`, `piper`, `sounddevice` have
no type stubs and weren't installed (the `dev` extra deliberately
excludes the `voice` extra — Phase 11's design, `voice_io.py` imports
these lazily specifically so the base install and test suite never need
them). Since CI's default install never has these packages either, these
3 errors would show up in every single CI run, permanently, regardless of
any real type issue — not signal. Fix: a `[[tool.mypy.overrides]]` block
setting `ignore_missing_imports = true` for `faster_whisper`, `piper`,
and `sounddevice`, matching what the project already does architecturally
(optional, lazily-imported, native deps) rather than fighting it.

### 24.2.3 Decision: `--strict` is real, but pre-existing debt doesn't block this phase

82 real errors (85 minus the 3 stub false-positives above) is a
meaningfully large set — several (`str | None` flowing into functions
typed as expecting `str`, mostly in `evaluator.py` and `orchestrator.py`
around `session_id` handling) look like places worth a real look rather
than a mechanical annotation fix, since they may point at an
already-validated-elsewhere invariant the type checker can't see, or an
actual gap. Bulk-fixing 82 errors across 14 files as a side effect of
"add a CI gate" is a bigger, riskier diff than this phase should carry —
it deserves its own review, and possibly its own phase, not a rubber
-stamped sweep bundled in here.

Instead: `[tool.mypy]` in `pyproject.toml` sets `strict = true` at the
top level, so **every file not explicitly listed below is held to full
strict mode starting now** — any new module, or any of the other 30
already-clean files, fails CI on a strict violation immediately. The 14
files with pre-existing errors each get a named
`[[tool.mypy.overrides]]` block:

```toml
[[tool.mypy.overrides]]
module = "viva.evaluator"
# 14 pre-existing strict-mode errors as of Phase 19 (2026-09-xx), mostly
# str | None flowing into str-typed SessionStore methods around
# session_id. Tracked in docs/plan.md's backlog, not fixed here -- see
# docs/system-design/24-phase-19-ci-quality-gates-design.md §24.2.3.
disable_error_code = ["arg-type", "union-attr"]
```

— one block per file, `disable_error_code` scoped to the specific codes
that file actually has (not a blanket `ignore_errors = true`, which would
also hide any *new* error of an unrelated kind introduced into that file
later). This makes the debt visible (14 named entries, each with a
count and a reason, not a silent exemption) and bounded (a new error code
in an overridden file still fails CI) while keeping this phase's actual
diff to configuration plus the mechanical/judged `ruff` fixes from
§24.1.3, not 82 speculative type fixes.

A backlog item goes in `docs/plan.md` for working through the 14-file
list, file by file, each as its own small reviewable patch — explicitly
not part of this phase's exit criteria.

## 24.3 `pytest-cov`

Baseline, measured directly: **95% line coverage, 3,267 statements, 157
missed, 641 passed** (`pytest --cov=src/viva --cov-report=term-missing`,
excluding the manual-only `tests/test_ingest_integration.py`). Genuinely
strong for a codebase this size — worth stating plainly rather than
treating coverage adoption as if starting from zero.

**Decision:** visibility first, as originally scoped in `plan.md`, plus
one guardrail: `--cov-fail-under=90` — five points below the measured
baseline, enough slack that normal development (a new file that takes a
few patches to reach full coverage, a legitimately-hard-to-test error
path) doesn't turn into CI noise, while still catching an actual
regression (a large new module landing with no tests at all, for
instance) before merge rather than after. Not a ratchet toward 100% —
the project's own testing philosophy already draws a line between
structural coverage and LLM-output-quality, and pytest-cov only measures
the former.

Coverage runs on the `pytest-py311`/`ubuntu-latest` leg only, not all six
matrix legs — coverage numbers don't meaningfully vary by OS or Python
version for this codebase, and running it six times over would just be
six times the CI minutes for the same number.

## 24.4 CI workflow changes

A new `lint` job in `.github/workflows/tests.yml`, independent of the
`pytest-py311` → `py312` → `py313` chain (no `needs:`, runs in parallel,
so a lint failure surfaces immediately rather than waiting behind three
sequential pytest legs). Single leg: `ubuntu-latest`, Python 3.11 (the
support matrix's floor — the most conservative check for any accidental
use of newer-than-3.11 syntax that `ruff`'s `UP` rules might otherwise
paper over). Two steps: `ruff check .` and `mypy src/viva`, both via the
`dev` extra so no new install step is needed beyond what already exists.

`pytest-py311`'s existing `run: pytest -q` step gains
`--cov=src/viva --cov-report=term-missing --cov-fail-under=90`; the other
five legs (`py311`/windows, `py312`/`py313` × both OSes) are unchanged.

## 24.5 Documentation update plan

| File | Change |
|---|---|
| `pyproject.toml` | new `[tool.ruff]` (select/ignore/per-file-ignores), new `[tool.mypy]` (strict + 14 per-module overrides); `dev` extra gains `ruff`, `mypy`, `pytest-cov` |
| `requirements.txt` | same three added to the `# dev/test` section, learned from Phase 18 not to let this drift again |
| `.github/workflows/tests.yml` | new `lint` job; `pytest-py311`'s run step gains `--cov` flags |
| `CONTRIBUTING.md` | new "Linting & type checking" section: how to run `ruff check .` / `mypy src/viva` locally, and how to read a per-module mypy override if a contributor hits one |
| `CHANGELOG.md` | `[Unreleased]` entry once implemented and verified |
| `docs/plan.md` | Phase 19 entry gets a `**Verified**` line once the exit criteria below are actually confirmed |

## 24.6 Test plan / exit criteria

- Clean `ruff check .` and `mypy src/viva` against current `main` after
  the §24.1.3 fixes and §24.2.3 overrides land — both commands exit 0.
- A throwaway branch reintroducing the Phase 18 `httpx2`→`httpx`-style
  typo (or any similarly-shaped dependency-manifest mistake) is
  confirmed to still pass `ruff`/`mypy` cleanly — this class of error was
  never one they'd catch (it's a `pyproject.toml` line, not Python code),
  confirming the honest scope of what this phase's gates do and don't
  cover, rather than assuming they'd have caught the earlier mistake.
- A real PR introducing one deliberate `ruff` violation (outside
  `E501`/the `cli.py` `B008` override) and one deliberate new `mypy`
  error in a currently-clean file, confirmed to block CI the same way a
  `pytest` failure already does.
- `pytest --cov-fail-under=90` confirmed to fail on a throwaway branch
  with a large chunk of test coverage deleted, and to pass on `main`
  as-is.

## 24.7 Deferred, not in scope for this phase

- **The 14-file mypy debt list** (§24.2.3) — tracked as a `docs/plan.md`
  backlog item, worked through file by file in follow-up patches, not
  this phase's exit criteria.
- **`ruff format` / `black` adoption and the `E501` reformat it implies**
  (§24.1.2) — a real follow-up worth its own design doc and sign-off,
  not bundled here.
- **A lockfile tool** (`pip-compile`, `uv lock`) — raised in Phase 18's
  design doc §23.7 as a natural fit for this phase; still true, still not
  done here. Worth its own decision (which tool, whether it replaces or
  supplements the two existing manifests) rather than folding in as an
  afterthought to the `ruff`/`mypy` adoption above.

## 24.8 Implementation finding: `tests/fixtures/golden_repos/` needed excluding

Found while actually applying the fixes above, not anticipated by §24.1's
measurements (which were scoped to `src/` and `tests/`, not the repo
root). `ruff check .` — the command this phase's CI job and
`CONTRIBUTING.md` both document — also scans
`tests/fixtures/golden_repos/`, small synthetic repos used as ingest test
data. These are deliberately not "clean" code (an unused import, for
instance, may be there on purpose to exercise the analyzer against it),
not this project's own source, mirroring exactly why
`[tool.pytest.ini_options].norecursedirs` already excludes them from test
collection. `ruff check . --fix --diff` (a dry-run preview, confirmed:
`--diff` combined with `--fix` doesn't write to disk) surfaced this
before anything was actually touched — one line added to `[tool.ruff]`'s
new `exclude` list closed it before running the real `--fix`.
