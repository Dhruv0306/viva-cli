# Contributing to viva-cli

Thanks for taking a look at this project. It's early-stage (see `docs/plan.md`
for the phased build plan), so process is intentionally lightweight — but a
few things keep the codebase coherent as it grows.

## Before you start

- Read `docs/requirements.md`, `docs/design.md`, and `docs/plan.md` first.
  This project is design-doc-driven: the docs are the source of truth for
  *what* to build and *why*, and code changes that contradict them should
  either update the docs in the same PR or be reconsidered.
- Check `docs/plan.md` for which phase is currently in progress. Work
  belonging to a later phase (e.g. adding RAG indexing before Phase 3/4 is
  reached) generally shouldn't be started early — see
  `docs/system-design/02-iteration-log.md` for why the phased sequencing
  exists (it's there to de-risk assumptions in order, not arbitrary).
- For anything that changes the *shape* of the system (new component, new
  data contract, changed state machine transition), open an issue first
  using the "Design question" issue template so it can be discussed against
  the existing design docs before code is written.

## Development setup

```bash
git clone https://github.com/<your-username>/viva-cli.git
cd viva-cli
python3 -m venv .venv
source .venv/bin/activate        # .venv\Scripts\activate on Windows
pip install -e ".[dev]"
cp .env.example .env
```

You'll also need [Ollama](https://ollama.com) running locally with the
configured models pulled:

```bash
ollama pull qwen2.5-coder:7b
ollama pull nomic-embed-text
```

## Running tests

```bash
pytest -q
```

Any change touching the timer, LLM client, or schemas should have a test
that exercises it directly — these are the components Phase 0 exists to
de-risk, and regressions there are expensive to catch late.

## Commit messages

See `.gitmessage` for the commit message template. To use it locally:

```bash
git config commit.template .gitmessage
```

Keep the summary line imperative and under ~72 chars ("add X", not "added X"
or "adds X"), and explain *why* in the body when the change isn't
self-evident from the diff.

## Pull requests

- Fill out the PR template completely, including the testing section — a
  PR without evidence of manual/automated testing against the relevant exit
  criteria in `docs/plan.md` will get sent back before review.
- Keep PRs scoped to one phase/concern where possible. A PR that mixes
  unrelated design-doc edits with implementation code is harder to review
  and harder to revert if something's wrong.
- Link the issue it resolves, if any.

## Code style

- Python 3.11+, type-hinted, `from __future__ import annotations` at the
  top of new modules.
- Prefer small, focused modules over large ones — this mirrors the
  component-boundary rule in `docs/design.md` §1 (components/modules
  shouldn't reach into each other's internals).
- No network calls in tests except through an explicitly mocked client —
  the test suite must run without Ollama installed or running.

## Reporting bugs / requesting features

Use the issue templates under `.github/ISSUE_TEMPLATE/`. Please search
existing issues first.
