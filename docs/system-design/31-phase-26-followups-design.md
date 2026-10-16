# System Design Reference -- Part 31: Phase 26 Follow-ups Design

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. Implementation design for `docs/plan.md`
> Phase 26. Written against `main` at commit `e3bfb01` (v0.5.1 released,
> Phases 24 and 25 merged).

Four backlog items from Phase 22 and 23's real runs, decided before the
planned full run. Each is its own patch so any of them can be dropped.

| # | Item | Decision |
| --- | --- | --- |
| 1 | The §27.7 token decision | Keep the token requirement. No skip-token opt-in. Harden what surrounds it. |
| 2 | Replenished questions repeat earlier topics on a small repo | Stop replenishing when only near-duplicates of asked questions remain. |
| 3 | No progress detail in the web UI during a long analysis step | Report per-file and per-module progress through a new optional UI hook. |
| 4 | Evaluator output quality, and the Phase 16 distance follow-up | Validate citations in code, tighten the feedback prompt, add a log summarizer. |

## 31.1 Why these decisions

Items 1 to 3 are code-level decisions with a clear best answer. Item 4 is
partly model behavior: code can reject a bad citation, but cannot judge
whether a sentence reads as feedback. §31.5 says exactly which parts are
and are not testable here.

## 31.2 Replenishment stops at near-duplicates (item 2)

`compute_replenished_plan()` rebuilt the plan with a higher ceiling and
returned the whole thing if it grew. On a one-module repo the new tail is
the same architecture topics a second time (Pass 1.5's extra slot) and
file-level items for the only file, so the candidate was asked about the
same code twice.

**Rule.** A new item is a near-duplicate of an asked question when they
share a key:

- architecture items: the key is `(category, architecture_topic)`;
- other items: `(category, target_module, target_file)`, except that in a
  module with exactly one source file a file-level item collapses to the
  module-level key, because it asks about the same code. In a multi-file
  module a file-level item is narrower, so it is kept.

Only `asked` and `answered` records count as asked: a question skipped for
no grounding was never put to the candidate. Duplicates within the new
batch collapse too, keeping the first.

**Ceiling escalation.** If a whole window of the rebuilt plan is
duplicates, the ceiling is raised again (up to 25 rounds) before giving
up, so a fresh item sitting behind duplicates is still found. `None` is
returned only when the planner returns fewer items than it was allowed,
meaning it has nothing left. That `None` already ends the session as
`QUESTIONS_EXHAUSTED`.

**Contract change.** `compute_replenished_plan()` gains an optional
`persisted` argument (every qa_record in the session). Without it the old
behavior is unchanged, so callers and tests that predate this phase work
as before. With it the function returns only the new items to save, not
the whole rebuilt plan. The orchestrator now reads the records once and
passes them in; its INFO lines are updated ("only near-duplicates of asked
questions remain", and the new total is computed from the items added).
Replenishment now reads `profile.sampled_files`, so five existing
orchestrator tests that passed a bare `object()` as the profile were
updated to pass a stand-in with that attribute.

Trade-off, stated: a session on a tiny repo now ends earlier than it did,
with unused time on the clock. That is the intended effect, and the user
asked for it.

## 31.3 The §27.7 token decision (item 1)

**Decision: keep the access token requirement for the container. Do not
add an opt-in that skips it.**

- The container binds `0.0.0.0` because Docker's port mapping needs it. It
  cannot see how the host published the port. An opt-in such as
  `VIVA_NO_AUTH=1` would be a claim the app cannot verify, and it is wrong
  after one edit to the compose file (`127.0.0.1:8000:8000` to
  `8000:8000`).
- What it would unlock is not harmless. The process holds `GITHUB_TOKEN`,
  clones repositories, and exposes cleanup and delete endpoints.
- Secure-by-default is the current practice for a service that can run
  somewhere other than the developer's own laptop. The friction (copy one
  token per container start) is real but small, and it is reduced below
  without weakening anything.
- Phase 25's smoke test now pins the behavior (401 without a token).

**Hardening that goes with it:**

1. `GET /` compared the `?token=` query value with `==`; the API
   middleware used `hmac.compare_digest`. Both now use the constant-time
   compare.
2. Every response carries `Referrer-Policy: no-referrer`,
   `X-Content-Type-Options: nosniff` and `X-Frame-Options: DENY`; the page
   that may embed the token and every `/api/*` response also carry
   `Cache-Control: no-store`. This stops the token leaking through
   `Referer`, being cached, or the UI being framed. No Content-Security-
   Policy is added: the page needs an inline script for the token, and a
   policy loose enough to allow it adds little.
3. The UI removes `?token=` from the address bar once the page has loaded
   and keeps the token in `sessionStorage` for the tab, so a reload still
   works and the secret is not left in history or a copied URL. Report
   download links still carry it in the query string, since an anchor
   cannot set a header; that is the one remaining use of a URL token.
4. `VIVA_ACCESS_TOKEN` (optional): use a fixed token instead of a random
   one per start. It must be at least 16 characters and contain only
   letters, digits and `_ - . ~` (it is embedded in an inline script and
   accepted from a URL, so nothing that could close the string or the tag
   is allowed); anything else is a startup error that never echoes the
   value. It is never printed, and blank or unset means generate as
   before. Setting it turns auth on even for a loopback bind, because
   setting a token is an explicit request for it and ignoring it quietly
   would be the less safe reading. It is read in `cli.py` and passed to `create_app(token=...)`
   (as `host` already is), so there is no new `Config` field. `docker-
   compose.yml` passes it through and `.env.example` documents it.

Not done: TLS (anything beyond a single machine should sit behind a
TLS-terminating proxy; the README already says the service is meant for
the loopback interface), token rotation, and moving report downloads to
`fetch` plus a blob.

## 31.4 Analysis progress (item 3)

The "Analyzing codebase" stage runs one LLM call per file, then one per
module, then one for the architecture summary, with nothing shown between
"Analyzing codebase..." and "Analysis complete". The web UI already polls
`stage` and `detail`, so the missing piece is only that nothing updates
`detail` during the stage.

- `analyze_repo()` gains an optional `on_progress(step, done, total)`
  callback, called after each file summary and each module reduce.
- `SessionUI` gains a **non-abstract** `stage_progress(stage, step, done,
  total)` that does nothing by default, so every existing subclass and test
  double keeps working. `WebSessionUI` overrides it to set `detail` to, for
  example, "Analyzing codebase: summarizing files 12/40...". No JavaScript
  change is needed, since the UI already shows `detail`.
- The terminal UI keeps its current output (no per-file lines), and the
  orchestrator logs progress at DEBUG.
- A progress callback that raises must not fail the analysis; it is
  wrapped, and the error is logged at DEBUG.

Out of scope: a determinate progress bar, and progress during indexing.

## 31.5 Evaluator output (item 4)

Two reported symptoms: citations like `(N/A (Not present in the policy
file))`, and weaknesses that read as plain facts about the code.

**Citations (testable).** The citation text came straight from the
model's `cited_file` field into the report, and `if point.cited_file`
treated any non-empty string as grounded, so "N/A" counted as a citation.
New `viva/citations.py`, `normalize_citation(raw, context)`, returns
`None` for:

- empty text, and phrases that say there is no citation ("N/A", "none",
  "not present...", "not applicable", "unknown", "no citation");
- when the context is given, a citation none of whose path or identifier
  tokens appears in the retrieved context (the model cited something it
  was never shown).

It is applied in `classify_answer()` and `generate_feedback()` before the
existing FR22 rules, so an "N/A" on a partial or incorrect verdict becomes
`needs_review` and an uncited entry is dropped, exactly as an empty
citation already was. `report.py` applies the context-free part as well,
so sessions already stored in a database render cleanly.

**Wording (not testable here).** The feedback prompt now says each
`missed` and `did_wrong` entry must say what the candidate's answer left
out or got wrong, written about the answer ("Did not mention that...",
"Said X, but the code does Y"), never a bare statement of fact about the
code, and gives one example of each. Whether `gemma4:e4b` follows this is a
property of the model; no pytest can show it. The planned full run is the
check, and §31.7 says what to look for.

**Phase 16 distance follow-up (instrumented, not decided).** The retrieval
log line already records per-question distances and result counts. What is
missing is a way to read them across a session. `scripts/
summarize_retrieval_log.py` parses those lines and prints, per category,
the number of questions, how many had no result, and min, median and max
distance. It makes the `error_handling` and `testing_strategy` question
answerable from the full run; it does not change `MAX_RETRIEVAL_DISTANCE`.

## 31.6 Testing and exit criteria

Each item adds unit tests that fail against the pre-change code (the
replenishment tests were confirmed failing first: 11 failed before the
change, all pass after). Full suite, `ruff` and `mypy` clean. No new
`Config` field.

## 31.7 What this cannot verify

- A browser: the address-bar cleanup and `sessionStorage` fallback in
  `app.js` are checked with a Node stub of `window`, `history` and
  `sessionStorage`, not a real browser.
- Docker: `VIVA_ACCESS_TOKEN` passing through compose is checked by
  `docker compose config` here and by CI's existing job; the smoke test
  runs with it unset.
- Model behavior: whether feedback is now phrased as gaps, and how often
  `needs_review` fires after citation validation. Look at these in the full
  run: weaknesses should read as what the candidate missed, no report line
  should contain "N/A", and `needs_review` should not jump on answers whose
  citations were fine.

## 31.8 Implementation notes

Four patches' worth of behavior, each with tests that fail against the
code before it (replenishment 11, progress 7; the citation, token and
header tests fail or cannot import against the old code).

- **Middleware order matters.** The security-header middleware is
  registered after the token middleware, because Starlette runs the most
  recently added one outermost. Registered first, the 401 the token
  middleware returns without calling the app skipped the headers; a test
  caught it.
- **Citation validation is strict on purpose.** A verdict whose citation is
  not in the retrieved context is now `needs_review`, which can flag more
  verdicts than before. That is the intended FR22 behavior, and §31.7 names
  it as the thing to watch.
- **Replenishment** reads `profile.sampled_files`, so five existing
  orchestrator tests now pass a profile stand-in with that attribute, and
  one log assertion changed to the new message.
- **The browser script** is tested under Node against stubs
  (`tests/test_token_init_js.py`), skipped when `node` is absent.
- `scripts/summarize_retrieval_log.py` has a test that feeds it the line
  `retrieval.py` really logs, so the regex cannot drift unnoticed.

Not changed: `MAX_RETRIEVAL_DISTANCE` (the summarizer needs the full run's
logs first), and no `Config` field was added.

