# System Design Reference — Part 28: Phase 23 Validation Follow-ups Design

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. Implementation design for `docs/plan.md`
> Phase 23. Written against `main` at commit `ff372b1` (v0.5.0 released).

Three small, independent fixes bundled the way Phases 14, 18 and 22
bundled theirs. Each comes from a real run during Phase 22's validation
(`27-phase-22-container-resumption-and-planning-split-design.md`
§27.11), none needs a model or a network to test, and none adds a
`Config` field (so no test-fixture ripple).

## 28.1 Item 1 -- ingest does not exclude a vendored JRE

**Observed:** the `Dhruv0306/Calculator_Java` repo vendors a `JRE/`
directory. Ingest descended into it, the session took about 7 minutes
before planning, and all four questions were about
`JRE/lib/security/default.policy` instead of the calculator code.

**Root cause:** `EXCLUDED_DIR_NAMES` in `ingest/filters.py` is an
exact, case-sensitive set of directory names (`vendor`, `node_modules`,
`build`, ...). It has no entry for a bundled Java runtime, so nothing
pruned it.

**Fix:** add `JRE`, `jre`, `JDK` and `jdk` to `EXCLUDED_DIR_NAMES`.

**Decisions, and what was not done:**

- *Exact names, not case-folding.* Folding case would also start
  excluding directories such as `Env`, `Out` or `Build`, which are
  legitimate source directories in some projects. Listing the four
  spellings of the runtime directories seen in practice is narrower and
  cannot drop real code by accident.
- *No new setting.* A user-configurable exclusion list would be more
  general, but it needs a new `Config` field, and `Config` has no inline
  defaults by design, so every test that builds one would need updating.
  Not worth it for one observed case; revisit if a second vendored-name
  case appears.
- *Only names with evidence.* Other conventional vendored names
  (`third_party`, `Pods`, `Carthage`, ...) were deliberately not added
  without a real case behind them.

## 28.2 Item 2 -- the report lists "None" as a finding

**Observed:** the `Calculator_Java` report's Weaknesses list contained
`None. The specific SecurityPermission grants listed are correctly
identified. (N/A)`. The model returned a non-finding as if it were a
weakness, and the report rolled it up verbatim.

**Root cause:** `ReportBuilder.build()` extends the strength and weakness
candidate lists with whatever strings the evaluation holds, with no check
that a string is a finding at all.

**Fix:** a small `_is_non_finding()` predicate in `report.py`, applied
where the candidates are collected (so it runs on the raw point text,
before a ` (file)` citation is appended, and on stored evaluations when an
old session's report is regenerated). It drops an entry whose text is only
a "nothing to report" statement:

- `None.`, `None`, `N/A`, `Nil.`, `Nothing.` (the word followed by
  punctuation or the end of the text);
- `None noted`, `None identified`, `Nothing found`, `None to report`,
  and similar;
- `No weaknesses identified`, `No significant issues`, and similar.

It is applied to both strengths and weaknesses. When a section ends up
empty, the existing `_None noted._` placeholder already renders.

**Precision matters:** a real weakness can legitimately start with the
word "None", for example `None of the error paths are tested.` The
patterns therefore require punctuation, the end of the text, or one of
the explicit follow-on words, and a test pins that `None of ...` is kept.

**Not done:** two other oddities in the same report are left alone
because they are about model output, not report assembly: weaknesses that
read as plain facts ("The grant includes ..."), and citations such as
`(N/A (Not present in the policy file))`. Neither can be fixed
deterministically without discarding real information; they belong with
evaluator prompt quality, which pytest cannot judge.

## 28.3 Item 3 -- plan replenishment is not logged

**Observed:** to tell whether the replenishment path ran in Phase 22's
real sessions, the question counts had to be compared by hand (2 to 4,
and 1 to 7). Nothing in the log says it happened.

**Fix:** `Orchestrator._replenish_plan()` logs one INFO line in each
outcome, with the session id:

- grew: `Replenished plan for session <id>: <old> -> <new> questions`;
- exhausted: `Plan replenishment found no new coverage for session <id>
  (<n> questions already planned)`.

The logging lives in the orchestrator wrapper, not in
`planning.compute_replenished_plan()`, which stays pure (Phase 22 §27.3).

## 28.4 Testing and exit criteria

- **Item 1:** parameterized tests that each of the four runtime directory
  names is pruned without descending, a Calculator-shaped tree keeps only
  its source file, and near-miss names (`jre-tools`, `jdkversions`,
  `JREADME`) are kept. The pruning cases were confirmed failing first.
- **Item 2:** report tests for each non-finding form being dropped from
  both lists, `None of ...` and ordinary findings being kept, and the
  empty section rendering `_None noted._`. The drop cases were confirmed
  failing first.
- **Item 3:** `caplog` assertions on both outcomes in
  `tests/test_orchestrator.py`, confirmed failing first.
- **Real hardware (Dhruv):** re-run a session on `Calculator_Java` and
  confirm ingest no longer includes `JRE/` (faster setup, questions about
  the calculator code), the report's Weaknesses has no "None" entry, and
  the log shows a replenishment line in a session long enough to
  replenish.
