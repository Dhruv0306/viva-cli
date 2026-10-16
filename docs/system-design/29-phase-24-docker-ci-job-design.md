# System Design Reference -- Part 29: Phase 24 Docker CI Job Design

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. Implementation design for `docs/plan.md`
> Phase 24. Written against `main` at commit `8e98791` (v0.5.1 released).

Phase 22's real Docker runs found several bugs that no test or CI job
could have caught, because CI never touched Docker. This phase adds a CI
job that builds the image and checks the two things those bugs had in
common: the resolved compose config, and what the image's `pip install .`
actually put on disk. It does not start the container; a smoke test is a
separate, later phase.

Scope was agreed before writing code: "option 2", meaning a compose
config check plus an image build plus a check of the installed package
inside the image.

## 29.1 What the job guards against

| Past bug | Where documented | Caught by |
| --- | --- | --- |
| `.env` copied from `.env.example` sets `OLLAMA_HOST=http://localhost:11434`, and compose passed it to the container, where `localhost` is the container itself | §27.10 | config check |
| Container port published without `127.0.0.1`, which is the premise the token decision in §27.7 rests on | §27.7 | config check |
| `host.docker.internal` unresolvable on native Linux without the `host-gateway` mapping | §26.2 | config check |
| `src/viva/web/static/*` dropped from the installed package | §26.9 | image check |
| `src/viva/analyzer/queries/*.scm` dropped from the installed package, so AST extraction silently fell back for every language | §27.8 | image check |
| `git` missing from the image, so ingest fails immediately | §26 (Dockerfile comment) | image check |

Not covered, on purpose: the `entrypoint.sh` CRLF problem (§26.12, only
reproduces from a Windows checkout, and the Dockerfile already strips the
`\r`), the access-token behavior of `viva serve` (already unit-tested), and
anything that needs a running container or Ollama. Those belong to the
smoke test phase.

## 29.2 Design

A new `docker` job in `.github/workflows/tests.yml`, on `ubuntu-latest`,
with no `needs:` (like `lint`), so a Docker regression shows up without
waiting for the pytest chain. Four steps:

1. `cp .env.example .env`, which is exactly what the README tells a user to
   do first. `.env` is gitignored, so this never touches the repo.
2. **Config check.** `docker compose config --format json` is piped into
   `scripts/check_compose_config.py`, with `OLLAMA_HOST=http://localhost:11434`
   also set in the shell (compose gives the shell priority over `.env`, so
   both paths are exercised). The script checks that the container gets
   `OLLAMA_HOST=http://host.docker.internal:11434`, that port 8000 is
   published on `127.0.0.1`, that the `host-gateway` mapping exists, and
   that the data and tree-sitter cache volumes are mounted.
3. `docker compose build`.
4. **Image check.** `scripts/check_image_assets.py` is piped to
   `python -` inside the freshly built image (`docker run -i
   --entrypoint python`), with the checkout's `src/viva/analyzer/queries`
   mounted read-only. It checks that `viva` is imported from `site-packages`
   and not from a source tree, that every static web file is present, that
   every `.scm` file in the checkout is present in the installed package,
   and that `git` is on `PATH`.

### 29.2.1 Decisions worth recording

- **The config JSON is never printed.** `docker compose config` prints
  `GITHUB_TOKEN` in plain text. The JSON goes straight into the checker,
  and the checker's messages never quote its input (a test pins this).
  `shell: bash` is set on that step so `pipefail` is on; without it a
  failing `docker compose config` would be hidden by the pipe.
- **The `.scm` list comes from the checkout, not a hardcoded list**, the
  same choice `release.yml` made, so a newly added language is covered
  without editing the script.
- **The checks are scripts, not inline YAML**, so the logic can be unit
  tested without Docker. They use only the standard library, so the job
  needs no Python setup step.
- **The image name comes from `docker compose config --images`**, not a
  hardcoded name, because compose derives it from the project directory.
- **The image check bypasses `entrypoint.sh`** (`--entrypoint python`). The
  entrypoint's privilege drop is not what is being checked, and running as
  root avoids needing a writable `./logs` and `./data`.
- **A vacuous pass is a failure.** If the mounted queries directory has no
  `.scm` files, the image check fails instead of comparing against nothing.
- **No new `Config` field**, so no test-fixture ripple.

## 29.3 Testing and exit criteria

- **Unit tests** (`tests/test_ci_scripts.py`, no Docker needed): a good
  config passes; each bug above is caught on its own, with exactly one
  problem reported; the checker never echoes a token or garbage input; the
  image check catches a missing static file, a missing `.scm`, a newly added
  source `.scm`, a source-tree import and an empty queries directory.
- **Against real tools in the authoring sandbox**, which has no Docker
  daemon but can run the standalone compose binary (`config` needs no
  daemon): the repo's own `docker-compose.yml` passes; reproducing the old
  `${OLLAMA_HOST:-...}` interpolation, the unqualified port, and a missing
  `extra_hosts` each fail with the expected message; the image check passes
  on a real non-editable install and fails on one built with the
  `package-data` section removed.
- **Exit criteria:** full suite, `ruff` and `mypy` clean; then the job runs
  green on GitHub for the push that adds it. That run is the only place
  `docker compose build` and `docker run` are exercised, since neither the
  sandbox nor the unit tests have a Docker daemon.

## 29.4 Out of scope

- Starting the container and calling its login or health endpoint, and
  anything touching Ollama or the ~22 MB tree-sitter grammar download. This
  is the planned follow-up (container smoke test).
- Publishing the image anywhere.
- Layer caching. A cold build is a few minutes; revisit if it becomes a
  problem.

## 29.5 Implementation notes

16 new tests in `tests/test_ci_scripts.py` (9 for the compose checker, 7
for the image checker). Full suite 728 passed, 1 skipped in the authoring
sandbox; `ruff` and `mypy` clean; coverage 95.43% against the 90% floor
(`scripts/` is outside the `src/viva` coverage measurement). `ruff check .`
does lint `scripts/`; `mypy` is only run on `src/viva`, as before.

Checked against real tools, as listed in §29.3: compose v5.6.0's
`config --format json` (no daemon needed) accepts the repo's compose file
and rejects reproductions of the old `OLLAMA_HOST` interpolation, the
unqualified port publish and a missing `extra_hosts`; the image checker
passes on a real non-editable install and fails, listing every static file,
on one built without `[tool.setuptools.package-data]`.

**Not verified here:** the sandbox has no Docker daemon, so `docker compose
build`, `docker compose config --images` against the real project name, and
`docker run -i --entrypoint python ... -` have not been run. The first run
of the job on GitHub is their real test, and a failure there is a new patch
on top, not a rebase. This section gets a results entry once that run is
seen.

