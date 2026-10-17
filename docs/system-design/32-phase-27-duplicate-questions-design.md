# System Design Reference -- Part 32: Phase 27 Duplicate Questions and Non-Answers Design

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. Implementation design for `docs/plan.md`
> Phase 27. Written against `main` at commit `5cb0ebf` (Phase 26 merged).

Phase 26's full run (two real sessions, 2026-10-17) showed that the same
question still reaches the candidate several times, and that a non-answer
is graded inconsistently. This phase fixes what the data shows and nothing
else.

## 32.1 What the real runs showed

**`viva-cli-test-c`** (one 13-line C file, every answer `x`). 14 questions
asked against 10 planned. The database shows the 4 extras were follow-ups
(`q_04_f1`, `q_06_f1`, `q_09_f1`, `q_05_f1`).

- Follow-ups fire on `partial` or `incorrect` (`_maybe_queue_followup`).
  The four parents were exactly the ones the model graded `incorrect` for
  the answer `x`; the ones it graded `not_attempted` got none. So the
  inconsistent grading of a non-answer directly produced extra questions.
- A follow-up drifted off its parent: the follow-up to "what is the hard-
  coded loop limit" asked about the function's purpose, the follow-up to
  "what value is passed as `i`" asked for the formula, and so on. Each
  landed on a fact another question had already asked, so "purpose",
  "calculation" and "return type" were each asked two or three times.
- Only `q_05_f1` is a near-verbatim rephrase of its parent.

**`throttle4j`** (4 of 10 answered). Questions 2 to 4 all asked about the
same `Metrics` class, so one mistake was graded three times and produced 7
near-identical weaknesses.

**What already existed, and why it did not help.** Duplicate *detection*
is not missing. Since Phase 6 the live loop embeds each generated question
and compares it with every asked one (`QUESTION_SIMILARITY_THRESHOLD`,
default 0.90, documented as never calibrated against real output), tries up
to three candidates, and **asks a duplicate anyway** if all three are
duplicates (§11.9: "never end the session early on this check"). On a tiny
repo every candidate is a duplicate, so the fallback fires every time. An
earlier proposal in this phase's planning to add a similarity layer was
therefore partly mistaken: the layer exists, and the two real problems are
that it is uncalibrated and that its fallback always asks.

## 32.2 A word-overlap layer under the embedding check

`viva/similarity.py`: Jaccard overlap of topic words (generic question
words and generic code nouns removed, camelCase and snake_case split,
plurals folded). It needs no model, so it works when the embedding
threshold is wrong.

It is deliberately conservative. Measured on the 14 real questions with the
final stopword list (one repository's worth of data, and the hard case
because every question shares the subject words): the two near-verbatim
rephrases scored 0.90 and 0.86, the best pair that is not a duplicate
scored 0.44, and one genuine paraphrased duplicate (the calculation, asked
two ways) scored 0.50. The threshold is **0.6**: well clear of the false
positives, at the price of leaving that last pair to the embedding layer.
Paraphrases ("role" against "purpose") are not caught here by design.

An earlier measurement with a hand-tuned stopword list suggested 0.5; the
final list is generic and the margin at 0.5 was only 0.06, so it was
rejected. The tests fix the 14 questions and assert exactly which pairs are
flagged.

## 32.3 A round of only duplicates is skipped once enough has been asked

In `_run_live_session`, a candidate is a duplicate if either layer says so.
If every candidate in a round is a duplicate:

- **fewer than 3 questions asked so far:** ask the fallback, as before. A
  similarity threshold that is too eager must never leave a session with a
  couple of questions (the lesson of §11.9);
- **3 or more asked:** mark each duplicate candidate
  `skipped_duplicate_question` (a new status, shown in the report as
  "skipped (duplicate of an earlier question)", counted as skipped in the
  summary) and loop. Nothing new left means replenishment finds no new
  coverage and the session ends.

Every skipped item stops being pending, so the loop cannot spin. Follow-ups
go through the same path, so a follow-up that only repeats an earlier
question is skipped too. This is the intended direction from Phase 26: end
early rather than repeat.

## 32.4 Calibrating the embedding threshold from real data

`scripts/question_similarity.py <db> <session-id-prefix>` embeds a
session's asked questions with the project's own embedding client and prints
every pair's cosine similarity next to its word overlap, sorted, with the
current threshold marked. It needs Ollama, so it runs on the user's machine.
The default 0.90 is not changed here: the table from the `test-c` session
says where duplicates and non-duplicates actually fall.

## 32.5 A non-answer is graded the same way every time

`viva/nonanswer.py`: `is_non_answer(answer, context)` is true for an empty
or punctuation-only answer, a run of one repeated letter (`xx`, `xxx`), a
standard placeholder ("idk", "I don't know", "no idea", "skip", "pass",
"n/a"), and a single letter that does not appear as a whole word in the
grounding context. The last rule is the reason it is not simply "short
answers": `i` is a correct answer to "what is the loop variable" and
appears in the code, so it goes to the model; `x` does not appear, so it
does not. Two-letter words ("no", "ok", "id"), answers containing digits,
symbols inside the word (`O(n)`) and anything of three or more letters
always go to the model, as does a placeholder that is itself a word in the
code (`pass` in a Python `except` block). A first version also rejected
two-letter answers; its own test showed that would reject "no".

`Evaluator.classify()` checks it first. A non-answer is recorded as
`not_attempted` with a fixed summary and a fixed improvement line, and the
record is completed immediately: no classification call, no feedback call,
no follow-up (follow-ups need `partial` or `incorrect`), and no invented
weakness such as "the candidate provided an irrelevant character".

## 32.6 Report: what is and is not changed

Changed:

- **Truncated text.** One weakness stopped mid-path at exactly 300
  characters, the schema's `maxLength`. The schema is passed to Ollama for
  constrained decoding, which closes the string at the cap. This is the most
  likely cause; it was not reproduced without a model. The prompt now asks
  for points under 200 characters and for the path to go in `cited_file`,
  not in the point; a point that still reaches the cap is shown with an
  ellipsis.
- **Citation repeated.** When the point already contains its citation, the
  report no longer appends it again.

Not changed, on evidence: no fuzzy de-duplication of weaknesses. Measured
on the 7 repeated `throttle4j` weaknesses, word overlap did not separate
same-point pairs from different-point pairs (a cross-group pair scored
0.45, the same as a same-group pair, and one same-group pair scored 0.13),
so it would either miss duplicates or merge different points. The fix for
those duplicates is upstream, in §32.2 and §32.3: do not ask the question
three times.

## 32.7 Log wording

`compute_replenished_plan()` returns `None` for two different reasons (the
planner ran out; only near-duplicates remained) and the Phase 26 log line
claimed the second. `planning.py` now logs the actual reason at INFO and
the orchestrator's line is neutral.

## 32.8 Testing and exit criteria

Each item adds tests that fail against the code before it. The similarity
tests fix the real 14 questions. Full suite, `ruff` and `mypy` clean. No
new `Config` field.

## 32.9 What this cannot verify

- **The embedding layer.** No embedding model in the authoring sandbox;
  0.90 is unchanged until the calibration table exists.
- **One session's worth of thresholds.** The 0.6 overlap threshold was
  measured on one repository. The margins are stated in the tests so a
  second session that disagrees will show it.
- **Model behavior.** Whether the prompt change stops long points from
  hitting the cap, and whether grading of real (non-trivial) wrong answers
  is consistent, need another run.

## 32.10 Implementation notes

Tests that fail against the code before them: the skip policy and the
lexical layer (3 orchestrator tests), the non-answer check (2 evaluator
tests), the report fixes (2 report tests), the log reasons (2 planning
tests). Full suite 894 passed, 1 skipped in the authoring sandbox; `ruff`
and `mypy` clean. No new `Config` field.

- **Two measurements were redone.** The first word-overlap threshold (0.5)
  came from a stopword list that happened to drop this repository's own
  words; redone with a generic list the margin was 0.06, so the threshold is
  0.6 (§32.2). A first non-answer rule also rejected two-letter answers; its
  own test (`no`) showed that was wrong (§32.5).
- **The floor of 3.** `_MIN_QUESTIONS_BEFORE_SKIPPING_DUPLICATES` is a
  module constant, not a setting. Changing it is a one-line decision once
  there is data on how often the embedding layer over-fires.
- **Stored status.** A session that ends because everything left was a
  duplicate is stored as `COMPLETE` like any other (the end-of-session
  overwrite described in the handoff), so the skipped records and the log
  are what show why it ended.
- `scripts/question_similarity.py` has not been run against a real
  database or Ollama here; its sorting, flags and rendering are unit tested
  with fake vectors.

