# System Design Reference — Part 10: Phase 5 QuestionGen Design

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts.

## 10.1 Scope

FR12–FR13: build a category-based coverage plan from the Project Profile,
then generate each question just-in-time, grounded in retrieved chunks.
FR14 (adaptive follow-ups) and FR15 (live duplicate-avoidance tracking)
are explicitly **not** implemented in this phase — see §10.3.

New package: `src/viva/questiongen/` (`models.py`, `planner.py`,
`retrieval.py`, `__init__.py`), mirroring `indexer/`'s shape. Public
entrypoints: `build_coverage_plan()` and `generate_question()` (plus
`generate_all()` for the smoke-test CLI command) — per `design.md`'s
component rule, these are the seams the Phase 6 Orchestrator will call.

## 10.2 Coverage Plan (FR12)

Five fixed categories: `architecture`, `implementation_detail`,
`tech_choice_rationale`, `error_handling`, `testing_strategy`.

Distribution, bounded by `config.max_questions` (existing tunable,
default 8):

1. **Pass 1** — one slot guaranteed per category. Module-scoped
   categories use the largest module by `file_count`; `architecture`
   never takes a `target_module` (see §10.4).
2. **Pass 2** — remaining slots distributed across the module-scoped
   categories, biggest modules first, until `max_questions` is hit or
   every (category, module) pair has been used.

A small repo with too few modules to fill every slot produces a
*shorter* plan rather than padding with duplicate (category, module)
pairs — `build_coverage_plan()` never fabricates coverage that isn't
there.

Follow-ups (FR14) are not part of this plan — see §10.3.

## 10.3 What Phase 6 Owns Instead

FR14 (adaptive follow-up depth) and FR15 (live duplicate/coverage
tracking across a session) are both **live-session** concepts: they need
to know what's already been asked and how the previous answer scored,
which requires session state that doesn't exist until Phase 6's
`IN_PROGRESS` state machine and SQLite persistence land.

Rather than inventing a session-shaped concept early, this phase's
contract to Phase 6 is intentionally narrow:

- `build_coverage_plan(profile, config) -> list[QuestionPlanItem]` — call
  once at `PLANNING`.
- `generate_question(plan_item, ...) -> GeneratedQuestion | None` — call
  once per plan item, live, during `IN_PROGRESS`.

`QuestionPlanItem.is_followup_of` is already part of the data contract
(§10.5) so Phase 6 can construct follow-up plan items using the exact
same shape once it has the session context (previous question id, and a
`generate_followup_question()`-style call that conditions on the prior
answer) to do so meaningfully. That call is Phase 6 scope, not built
here — this phase has nothing to condition a follow-up on.

## 10.4 Retrieval Quality (resolving open question #6)

`04-open-questions.md` item 6 found that embedding a bare
category/module string (e.g. `"cli"` against `pallets/click`) collided
lexically with unrelated test-fixture chunks rather than semantically
matching real implementation code. Two mitigations, per the earlier
design discussion:

1. **Query reformulation (primary fix).** `retrieval.build_query()`
   never embeds a bare category/module name — it expands the category
   into a template phrase and appends the target module's own
   `ModuleSummary.summary` text as `Context: ...`. This carries real
   domain vocabulary into the query instead of a short, collision-prone
   string. Deterministic, no extra LLM call.
2. **Test-path post-filter (belt-and-suspenders).** Even a well-formed
   query can surface test chunks ahead of implementation chunks for a
   thin module. `retrieval._is_test_path()` — a heuristic mirroring
   `ingest/sampling.py`'s `_is_test_file()`, adapted to operate on the
   `filepath` string Chroma's metadata carries rather than a `Path`
   (see module docstring for why this isn't a direct cross-component
   import) — deprioritizes/excludes test-path chunks for every category
   except `testing_strategy`, where test chunks are exactly what should
   be preferred. Over-fetches (`n_results = top_k * 3`) before filtering
   so the exclusion doesn't starve the final result set, and falls back
   to the unfiltered candidates if filtering would leave zero results
   (a thin module where the only close matches are tests must still
   produce *a* grounded question, not none at all).

**Resolution of open question #6:** addressed in Phase 5 via (a) from
the open-questions doc's candidate list (query reformulation), plus a
lightweight version of (b) (reranking) as a low-cost complement, rather
than (c) (alternate `EMBEDDING_MODEL` pressure-test) or (d) (accepting
the noise). If this combination still surfaces test-fixture-sourced
questions for `implementation_detail`/`tech_choice_rationale`/
`error_handling` categories on a real repo, (c) is the next candidate to
revisit — not blocking this phase.

## 10.5 Data Contracts

```python
@dataclass(frozen=True)
class QuestionPlanItem:
    id: str
    category: QuestionCategory  # architecture | implementation_detail |
                                 # tech_choice_rationale | error_handling |
                                 # testing_strategy
    target_module: str | None   # None for `architecture`
    status: Literal["pending", "generated", "skipped_no_grounding"] = "pending"
    is_followup_of: str | None = None  # unused until Phase 6

@dataclass(frozen=True)
class GeneratedQuestion:
    plan_item: QuestionPlanItem
    question_text: str
    grounding_chunk_ids: list[str]
```

Matches the "Question Plan Item" shape already specified in `design.md`
§6 — this is where it becomes real code.

## 10.6 LLM Call Shape

`generate_question()` is **free text, not schema-validated** — one call
per question (FR13's "just-in-time" framing implies one question at a
time, not a batch-of-N-questions call), same rationale
`llm_client.py` already gives for `summarize_file`/`reduce`: nothing
downstream parses this as structured data. The 3-layer structured-output
reliability strategy stays reserved for machine-consumed output
(`evaluate_answer`), where a malformed response would actually break a
downstream parser.

Grounding (FR13) is enforced by construction, not by asking the model to
self-report it: the caller always supplies real retrieved chunk text as
`[CODE_CONTEXT]`, and a plan item with zero retrieved chunks is skipped
(`status="skipped_no_grounding"`) rather than generating a question with
empty/fabricated context.

## 10.7 Real-Repo Bug: Non-Source Modules Winning Module Selection

Found via a real `viva questiongen https://github.com/pallets/click` run
on Windows + Ollama (not caught by the automated suite — the checked-in
golden repos don't have a `tests/`/`docs/` directory that outsizes the
real source directory, so the failure mode never triggered against
them). Same category of miss as three of Phase 3's bugs: real-repo
testing is what surfaced it, matching the project's established "real
Windows testing is non-negotiable" principle.

**Symptom:** every generated question — regardless of category —
grounded in `docs/*.md` or `tests/*.py` chunks, never in `click/core.py`
or `click/parser.py`, click's actual implementation.

**Root cause:** `ModuleSummary.module` is literally the top-level
directory name (`ingest/sampling.py::_top_level_module`) — established
back in Phase 2/3, where it never mattered whether `"tests"` or
`"docs"` counted as a "module" the same as real source code. Phase 5's
planner was the first consumer to treat "biggest module by
`file_count`" as if it always meant "richest source-code module." For
click specifically, `tests/` (45 files) and `docs/` (38 files) both
outsize the actual `click/` package (25 files), so every module-scoped
category's Pass 1 target became `tests` or `docs`. The `where={"module":
target_module}` retrieval filter then locked grounding to that
directory *before* `retrieval.py`'s test-path post-filter ever got a
chance to help — the filter was correctly excluding test-path chunks
from a query that had already been restricted to nothing but test-path
chunks.

**Fix** (`planner.py`):
- `_is_source_module()` excludes a known non-source top-level directory
  vocabulary (`tests`/`docs`/`examples`/`scripts`/`benchmarks`/`vendor`/
  `dist`/`build`/`target`/the loose-root-files `""` bucket) from the pool
  `implementation_detail`, `tech_choice_rationale`, and `error_handling`
  pick their target module from. Falls back to the unfiltered module
  list only if *every* module in the profile is non-source (a docs-only
  or examples-only repo shouldn't be left with literally nothing to
  target).
- `testing_strategy` is no longer just another module-scoped category
  distributed across source modules in Pass 2 — it explicitly targets a
  test-like directory (`tests`/`test`/`__tests__`/`spec`/`specs`) when
  one exists, since pairing it with e.g. `target_module="click"` would
  filter retrieval to a directory containing zero test chunks for a
  click-shaped repo. Falls back to the largest source module (relying on
  `retrieval.py`'s existing test-path *preference*, not exclusion, for
  that category) only when no dedicated test directory exists — e.g. a
  Go-style repo with co-located `*_test.go` files.

**Second instance of the same bug, found on the same run:** the first
fix's denylist correctly excluded `tests`/`docs`/`examples`, but click
also has a `.github` directory (workflow YAML), which the hardcoded
vocabulary hadn't anticipated — `q_06`–`q_08` in the first fixed run
still targeted `.github` once Pass 2 exhausted `src` (click's actual
package, correctly identified this time thanks to the first fix, since
click uses a `src/`-layout). A hardcoded denylist will always miss the
*next* repo's specific tooling directory name, so the fix generalizes
instead of adding one more entry: `_is_source_module()` now excludes any
dot-prefixed top-level directory unconditionally (`.github`,
`.circleci`, `.vscode`, `.git`, ...), which covers essentially every
ecosystem's CI/tooling convention in one rule rather than playing
whack-a-mole per repo.

**Regression tests** (`test_questiongen_planner.py`): a click-shaped
profile (`tests` > `docs` > `click` > `examples` by file count) asserts
the three implementation-style categories all target `click`; a second
profile where `click` *is* the largest module still asserts
`testing_strategy` targets `tests` specifically, not `click`; a third
covers the co-located-tests fallback; a fourth covers the
all-modules-non-source degrade path; a fifth reproduces the exact real
click module shape (`src`/`tests`/`docs`/`examples`/`.github`) and
asserts `.github` is never picked; a sixth covers vendored/build
directories (`node_modules`, `dist`).

## 10.8 File-Level Fallback (Pass 3)

Design decision made after the click bugfix round, not itself a bug fix.
With `docs`/`examples`/`.github` correctly excluded and `tests` reserved
for `testing_strategy`, click only has **one** real source module (`src`)
— Pass 2 has nothing left to distribute extra slots to, so the plan
stopped at 5 items instead of `MAX_QUESTIONS`'s default 8.

Two options were on the table: leave a shorter, honest plan for
thin-module-count repos, or fill remaining slots some other way.
Decision: fill them, combining both remaining candidates —

- **Multiple questions per module** once modules run out, *and*
- **File-level targeting** for granularity, rather than repeating the
  same broad module-level question.

**Pass 3** (`planner.py`): once every source module already has a
module-level item (Pass 1/2) and slots still remain, rank each source
module's non-test files (`always_include` files — README/entry point/
manifest, already flagged by Ingest — first, then largest-by-`size_bytes`
as a coarse "substantial implementation" proxy; no richer per-file
signal survives to the Project Profile, since import-graph centrality is
ephemeral to Phase 2's sampling ranking and never persisted onto
`SampledFile`) and produce additional `implementation_detail`/
`tech_choice_rationale`/`error_handling` items scoped to a specific file
(`QuestionPlanItem.target_file`), cycling module → file-rank → category
the same round-robin way Pass 2 cycles modules, until `max_questions` is
hit or every source module's file pool is exhausted. `architecture` and
`testing_strategy` are unaffected — both already have dedicated,
non-distributed targeting from Pass 1.

`target_file` set means grounding narrows all the way to that file:
`retrieval.py`'s `where` clause becomes `{"filepath": target_file}`
instead of `{"module": target_module}`, and `build_query()` appends
"Focus specifically on the file `{target_file}`." to the reformulated
query so retrieval doesn't just fall back to the module's most
generically-relevant chunks. `LLMClient.generate_question()` gained a
matching optional `[TARGET_FILE]` prompt section, included only when
set — same explicitly-labeled-sections convention as `[CATEGORY]`/
`[TARGET_MODULE]`.

Verified against the real click module/file shape: with `src`/`tests`
correctly identified as the only source modules, Pass 3 fills the
remaining 3 slots with `implementation_detail`/`tech_choice_rationale`/
`error_handling` items all targeting `src/click/core.py` (the largest
non-test file in `src`) — 8 questions total, none reaching into
`docs`/`examples`/`.github`.

## 10.9 Question Phrasing: Length/Clause-Count Tuning

Found by inspection after the click bugfix rounds, not a grounding bug —
every generated question was structurally correct (single question,
grounded, no preamble) but tended toward long, multi-clause phrasing:
"how does X ensure Y, especially when Z, given that W...". Root cause:
`QUESTION_GEN_SYSTEM_PROMPT` had no length or clause-count constraint at
all, and `generate_question`'s `target_tokens=80` (→ ~240 tokens of
`num_predict` headroom via `_generate`'s 3x-with-floor rule) is nowhere
near tight enough to force brevity as a backstop — the "be specific,
never generic" instruction alone was enough to push the model toward
stacking qualifiers to prove specificity.

**Fix:** added an explicit "ONE clause, ONE sentence, roughly 15-25
words" constraint to the system prompt, plus a concrete good/bad example
pair contrasting a single-clause specific question against a
multi-qualifier one (using the exact `_resolve_context`/`chain`/
`resilient_parsing` case from the real click run as the "bad" example).
`target_tokens` deliberately left unchanged — tightening the token cap
as the primary fix risks truncating mid-sentence for models that don't
fully respect `think=False` and pad before settling into the answer (see
`_generate`'s existing comment); the prompt instruction is the right
lever, the token cap stays a generous backstop.

Not verifiable by the mocked test suite (fake `LLMClient`s don't
exercise real model instruction-following) — `test_llm_client.py` only
guards against the constraint text regressing out of the prompt.
Confirming the actual output improved needs another real
`viva questiongen` run.

## 10.10 CLI Markup Bug (Rich)

Same real-repo run surfaced a second, smaller bug: every question's
`id [category / module]` label was silently missing from the printed
output. Root cause: the label was built as a bare
`f"[{category} / {module}]"` string passed straight into
`console.print()` — Rich's `Console.print()` treats `[...]` as markup
syntax, not literal text, by default. `"[implementation_detail /
click]"` isn't a recognized style name, so Rich silently dropped it
rather than printing it or raising.

**Fix:** switched the label to parentheses (`f"({category} /
{module})"`), which carries no markup meaning to Rich. Scoped narrowly
to this one label — the pre-existing pattern of interpolating
LLM-produced free text (architecture summaries, module summaries)
directly into `console.print()` calls elsewhere in `cli.py` carries the
same theoretical risk if that text ever contains a literal `[`, but that
pattern predates Phase 5 and isn't this PR's regression to fix.

**Regression test** (`test_cli_questiongen.py`): asserts the literal
`"implementation_detail / src"` and `"architecture / (project-level)"`
strings appear in `viva questiongen`'s stdout.

## 10.11 CLI Smoke-Test Command

`viva questiongen <repo_url>` — same precedent as `viva analyze`/`viva
index`: clone → ingest → analyze → index → build the coverage plan →
generate every question in it → print `id [category / module]`,
question text, and grounding chunk ids. This is what the Phase 5 exit
criteria ("generated questions manually reviewed against test repos for
grounding accuracy and category coverage") needs a command to *do*. Not
the real `viva start` — same caveat as every prior phase's smoke-test
command.
