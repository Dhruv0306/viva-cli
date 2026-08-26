# 08. Phase 3 Analyzer: Implementation Design

Design decisions locked in before Phase 3 implementation began, covering
the choices `docs/plan.md`'s Phase 3 entry ("tree-sitter extraction
(FR6), Map-reduce Project Profile generation (FR7)") leaves open.

## 8.1 Tree-sitter dependency: `tree-sitter-language-pack`, not per-language packages

Two viable approaches were considered:

- **(a)** `tree-sitter` core + `tree-sitter-language-pack` — a single
  actively-maintained aggregator exposing `get_language(name)` /
  `get_parser(name)` with prebuilt wheels for hundreds of grammars,
  including every language in the FR6 allowlist.
- **(b)** Pin nine individual grammar packages (`tree-sitter-python`,
  `tree-sitter-javascript`, `tree-sitter-typescript`,
  `tree-sitter-java`, `tree-sitter-go`, `tree-sitter-rust`,
  `tree-sitter-c`/`tree-sitter-cpp`, `tree-sitter-ruby`,
  `tree-sitter-c-sharp`) directly.

**Decision: (a).** The older aggregator (`tree_sitter_languages`) is
explicitly unmaintained and its own README points at
`tree-sitter-language-pack` as the successor. One dependency avoids nine
separate version-pin surfaces that each need to stay compatible with the
core `tree-sitter` package's version; the cost (hundreds of unused
language entries bundled in) is a non-issue for a local CLI tool. If the
aggregator's release cadence ever lags a language's real grammar release,
that's the point to revisit, not before.

Verified against a real install: `csharp` (not `c_sharp`) and `tsx` (a
distinct grammar from `typescript`, needed for JSX inside `.tsx` files)
are the language-pack's actual keys — both confirmed via
`manifest_languages()` before being hardcoded into `languages.py`.

## 8.2 Extraction mechanism: `.scm` query files per language

Tree-sitter's own declarative Query DSL (one `.scm` file per language,
captures named `definition.function` / `definition.class` /
`name.function` / `name.class`) rather than hand-walking AST node types
in Python with per-language `if` branches. This is tree-sitter's own
idiomatic approach (the same mechanism `nvim-treesitter`, `tree-sitter
tags`, etc. use) and keeps each language's extraction logic in one small,
inspectable file instead of a 9-way branch in Python.

Every query in `analyzer/queries/` was compiled and pattern-matched
against real sample code for its language before being trusted — this
surfaced the exact node-type names per grammar (e.g. Go's
`method_declaration` uses a `field_identifier` for the receiver method
name, not `identifier`; C++ function declarators use `field_identifier`
inside a class but `identifier` at top level, so `cpp.scm` needs both
patterns).

**Docstring extraction:** Python's docstring convention (a bare string
literal as the first statement in a function/class body) is
structurally different from every other language in the allowlist, which
use a doc-comment convention instead (JSDoc/Javadoc/rustdoc-style: a
`comment` node immediately preceding the definition). `extract.py`
handles Python as an explicit special case and treats "immediately
preceding `comment` sibling" as the general rule for every other
language — every grammar in the allowlist uses `comment` as the node
type for both line and block comments, so this one rule covers JS, TS,
Java, Go, Rust, C, C++, Ruby, and C# without further per-language
branching.

## 8.3 `ProjectProfile` unification: built now, not deferred

`docs/design.md` §6 specifies the full Project Profile schema, but
`ingest/models.py`'s `IngestResult` only ever covered Ingest's half of
it — `architecture_summary`, per-module `summary`, and
`test_coverage_present` were left as an explicit TODO for "the Analyzer
(Phase 3)."

**Decision: introduce `viva/profile.py`'s `ProjectProfile.build()` now,**
merging `IngestResult` and the new `AnalysisResult` into the single FR8
artifact, rather than deferring assembly to the Phase 6 Orchestrator.
Delaying this would mean every later phase (RAG indexing, question
generation, evaluation, reporting) either imports two separate objects
or waits for a second unification pass — better to have the real,
tested shape flowing out of Phase 3 once, since FR8 already fully
specifies it and nothing about the shape is Phase-6-specific.

`ProjectProfile` lives at the top level (`src/viva/profile.py`), sibling
to `config.py`/`schemas.py` — deliberately not owned by `ingest/` or
`analyzer/`, since FR8 requires it be "stored separately from the
retrieval index and injectable into any LLM call," making it a
first-class pipeline artifact in its own right rather than a private
detail of either component that produces half of it.

## 8.4 Golden-repo fixture strategy for the hierarchical-reduce path

`docs/plan.md`'s Phase 3 exit criteria requires "at least one test repo
with enough modules to force the hierarchical reduce path, not only
small repos where a single flat reduce suffices." Two ways to get there,
used together for different purposes:

- **Artificially low `MAX_REDUCE_CONTEXT_TOKENS`** in unit tests
  (`tests/test_analyzer_reduce.py`, `tests/test_analyzer_integration.py`)
  — fast, deterministic coverage of the recursion logic itself
  (batching, batch-of-batches, termination) against the existing small
  fixtures. Doesn't need a large repo; the point is to exercise the code
  path, not judge summary quality.
- **`tests/fixtures/golden_repos/py_medium/`** — a synthetic ~12-module
  Python project (12 feature modules + root + `tests/`, 14 module groups
  total, 28 files), checked in specifically to exceed
  `MAP_REDUCE_BATCH_SIZE`'s default of 8 *without any config override* —
  proving the hierarchical path fires under real, unmodified defaults,
  not only when artificially forced. `test_analyze_repo_py_medium_fixture_forces_hierarchical_reduce_with_real_config`
  in `tests/test_analyzer_integration.py` asserts this directly. This is
  also the fixture to point `viva analyze` at for the exit criteria's
  manual profile-quality review, once a real Ollama model is available
  to run it against (the automated test suite only exercises the code
  path with a fake LLM client, not summary *quality*).

## 8.5 Other decisions (lower-stakes, standard-practice defaults)

- **`LLMClient` API shape:** one generic `reduce(label, summaries,
  target_tokens)` method reused at every level (per-module reduce, and
  every batch/recursion level of the architecture summary), plus a
  `summarize_file(...)` method for the Map step. `docs/system-design/06-cli-contract-and-profile-scaling.md`
  §6.2 itself frames hierarchical reduce as "standard tree summarization,
  not a novel mechanism" — one reused method mirrors that framing rather
  than three near-duplicate ones.
- **No structured-output reliability strategy for summaries:** the
  3-layer strategy (grammar-constrained decoding → Pydantic validation →
  repair loop → `needs_review` fallback,
  `docs/system-design/01-resolved-decisions.md` §1.2) exists to protect
  the Evaluator's machine-consumed verdicts. A prose summary has no such
  downstream parsing to protect, so `summarize_file`/`reduce` return
  plain strings with no schema.
- **Token counting:** a cheap chars/4 heuristic (`analyzer/tokens.py`),
  not a real per-model tokenizer. `MAX_REDUCE_CONTEXT_TOKENS` is already
  documented as "a conservative default... to leave room," so an
  approximate-but-conservative estimate is consistent with that intent,
  and avoids depending on tokenizer compatibility with whichever model
  `LLM_MODEL` is currently set to.
- **Runtime default for `MAX_REDUCE_CONTEXT_TOKENS`:** when unset,
  `LLMClient.get_context_window()` makes a best-effort attempt to read
  the configured model's real context window via `ollama.Client.show()`
  and uses half of it; if that's unavailable (older Ollama, unusual
  model metadata shape, no server reachable yet), a hardcoded
  conservative floor of 3000 tokens is used instead. This is
  deliberately not a required/abstract method on `LLMClient` — a test
  double doesn't need to implement it, since `None` is a valid,
  handled return.
- **`test_coverage_present`:** derived deterministically as `any(f.is_test
  for f in sampled_files)`, reusing Phase 2's existing per-file tagging —
  no LLM call needed.
- **Parse-failure/fallback bookkeeping:** `AnalysisStats` mirrors Phase
  2's `ExclusionStats` pattern (ast-parsed count, line-window-fallback
  count, per-language parse-failure count), feeding the same kind of
  transparency `docs/design.md` §8.1 gives as an example ("12 files fell
  back to line-window chunking").
- **`LINE_WINDOW_SIZE`/`LINE_WINDOW_OVERLAP`:** added to `Config` as
  real tunables (default 60/15, matching
  `docs/system-design/05-repo-lifecycle-and-language-coverage.md` §5.1's
  stated default), following Phase 1's established FR28 convention that
  every numeric tunable gets a validated environment variable rather
  than being hardcoded.
- **Concurrency:** sequential Map-step LLM calls for v1. Ollama serves
  one local model at a time on typical hardware, so concurrency here
  wouldn't reduce wall-clock time and isn't called for by Phase 3's
  scope.

## 8.7 Real-model finding: thinking models need `think=False`, not just a bigger `num_predict`

Running `viva analyze` against a real Ollama model surfaced almost every
file/module summary coming back as an **empty string**, with one reduce
call producing a visibly confused response ("Please provide the
summaries you would like me to synthesize...") -- which only makes sense
if the individual summaries it was asked to combine were themselves
empty.

Root cause: `summarize_file`/`reduce` cap `num_predict` (`evaluate_answer`
doesn't -- it lets generation run unbounded until the model finishes).
A reasoning/"thinking"-capable model spends that capped budget on hidden
`<think>...</think>` reasoning before ever emitting visible `content`,
so generation gets cut off mid-thought and the visible content comes
back empty. `evaluate_answer` never hit this because nothing bounds how
long it can think before answering.

Fix: pass `think=False` explicitly on every `summarize_file`/`reduce`
call (the installed `ollama` client — 0.6.2 at the time of this fix —
exposes `think: bool | "low" | "medium" | "high"` on `Client.chat()`).
This is a no-op for non-reasoning models, so it's safe regardless of
which model is configured. `num_predict`'s multiplier was also bumped
from 1.5x to 3x the target length as defense-in-depth, in case a given
model doesn't fully respect `think=False`. As a second line of defense,
an empty response after `.strip()` no longer propagates silently -- it's
logged at `WARNING` with the model name and generation params, and a
placeholder string is returned instead of `""`, so a summarization
failure is visible in the profile output rather than silently producing
a blank `architecture_summary`/`modules[].summary`.

`evaluate_answer` was deliberately left unbounded rather than also
getting `think=False` -- it already works correctly (verified via the
Phase 1 pressure-test harness), and changing working, tested behavior to
fix an unrelated bug elsewhere isn't the right move without its own
verification pass.

## 8.6 `modules[]` schema: scoped down from the original draft

`docs/design.md` §6's original draft specified each `modules[]` entry as
`{"path": "src/auth", "role": "authentication", "key_files": [...],
"summary": "..."}`. The Phase 3 implementation ships
`{"module": "auth", "summary": "...", "file_count": 4}` instead — this
was noticed only while writing this doc, after the rest of the
implementation was already built and tested, not decided up front the
way the rest of this document's choices were. Flagging it explicitly
here rather than letting the schema doc silently drift out of sync with
the code, per the project's "docs must stay in lockstep" principle.

**What was dropped and why it's an acceptable v1 scope cut:**
- **`path` → `module`:** `SampledFile.module` (Phase 2's directory-
  stratified sampling grouping) is already a top-level-directory name,
  not a full nested path — using it directly avoids inventing a second,
  redundant notion of "module boundary" in the Analyzer that would need
  to agree with Sampling's. A real nested `path` would need the Analyzer
  to compute its own module boundaries independently of Phase 2's, which
  duplicates logic for no clear benefit at this stage.
- **`role` (a short classification like "authentication"):** dropped
  because assigning it well needs either its own LLM call per module
  (extra cost, extra prompt to get right) or a keyword-heuristic that
  would likely be wrong often enough to be misleading in a profile
  that's meant to be trustworthy context for every later phase. Worth
  revisiting once there's a concrete downstream consumer (e.g.
  QuestionGen category-to-module targeting) that would actually use it.
- **`key_files` (representative files per module):** dropped for v1;
  the closest existing building block is Phase 2's `always_include`
  sampling tier, but that tier mixes manifests/READMEs/entry points with
  genuinely representative source files, so it isn't a clean fit without
  its own selection logic.

**What adding these back would take**, if a later phase needs them:
`role` would need either a small additional LLM call in `reduce_module()`
(cheap: one short classification call per module, same call pattern as
`summarize_file`) or a keyword-matching heuristic against the module's
file summaries. `key_files` could be derived cheaply and deterministically
from each module's `FileSummary` list — e.g. the files with the most
`CodeUnit`s, or the ones tagged `always_include` by Phase 2 — without
needing another LLM call at all.
