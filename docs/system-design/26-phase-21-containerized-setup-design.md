# System Design Reference — Part 26: Phase 21 Containerized Setup Design

> Part of the full system-design reference. See `README.md` in this folder
> for the complete set of parts. Implementation design for `docs/plan.md`
> Phase 21. Written against `main` at commit `82eaf55` (Phase 20 complete).
>
> **A real constraint on this doc, stated up front:** Docker itself isn't
> available in the environment this was written in -- every other design
> doc in this series (18-20) verified its claims by actually running the
> real command, the real test, the real client. This one can't do that
> for the Dockerfile/compose files themselves; what's verified below is
> everything *about the target environment* that's checkable without
> Docker (the real `git` binary requirement, the real caching behavior of
> `tree-sitter-language-pack`, the real loopback-check logic), and the
> Dockerfile/compose content itself is a best-effort design, not a tested
> one. Real-world validation on real Docker -- this phase's exit criteria
> -- carries more weight than usual as a result.

## 26.1 The open question `plan.md` left for this doc: decided

`plan.md`'s Phase 21 entry asked whether the container bundles Ollama or
expects it external, and already leaned toward external. Deciding it for
real, as the senior-dev call this doc exists to make:

**External.** The container runs `viva serve` only; `OLLAMA_HOST` points
at an Ollama instance the person already has running, in whatever way
they already run it. Reasons, beyond the original bullet's GPU-passthrough
point:

- Ollama's own official Docker image already exists and is what anyone
  wanting *Ollama* containerized would reach for directly -- reinventing
  that inside this project's image would be maintaining a fork of
  someone else's packaging for no benefit.
- `viva-cli`'s container needs no GPU access at all with this split --
  it only ever makes HTTP calls to whatever `OLLAMA_HOST` points at, GPU
  or not, local or remote. Bundling Ollama would drag GPU passthrough
  (`--gpus all`, host driver version matching, entirely different
  concerns on Docker Desktop vs. native Linux Docker) into a phase about
  removing onboarding friction, not adding a new category of it.
- Most people evaluating this tool for the first time (this phase's
  actual audience, per `plan.md`'s own root-cause statement) already
  have Ollama running natively for other things -- that's the whole
  premise of a "local-first" tool. Pointing a lightweight container at
  an existing Ollama is less friction than asking them to run two
  separate Ollama instances (their existing native one, plus a
  containerized one this image would bring along).

## 26.2 `OLLAMA_HOST`'s default doesn't work inside a container

`Config.ollama_host` defaults to `http://localhost:11434`
(`config.py:244`). Inside a container, `localhost` resolves to the
container itself, not the host machine running Ollama -- confirmed by
reading the actual default, not assumed to "probably need documenting."
This is the single most important thing a person following this phase's
README section needs to get right, and it differs by platform:

- **Docker Desktop (Mac/Windows):** `http://host.docker.internal:11434`
  works out of the box -- Docker Desktop provides this DNS name
  automatically.
- **Native Linux Docker:** `host.docker.internal` does **not** resolve by
  default. Either add `extra_hosts: ["host.docker.internal:host-gateway"]`
  to the compose file (Docker Compose v2.x supports this directly,
  translating to `--add-host` under the hood) so the same
  `OLLAMA_HOST=http://host.docker.internal:11434` value works
  everywhere, or use the host's actual LAN/bridge IP directly. Decision:
  use `extra_hosts` in `docker-compose.yml` so one `OLLAMA_HOST` value
  works across all three platforms without per-platform README branches
  -- one fewer thing to get wrong, at the cost of one extra compose
  stanza.

## 26.3 `viva serve --host 127.0.0.1` (the CLI default) is also wrong here

Same class of problem as §26.2, different layer: `viva serve` defaults to
`--host 127.0.0.1` (`cli.py`). Binding to loopback *inside* a container
makes the server unreachable via Docker's port mapping from the host,
even with `-p 8000:8000` -- the container's own `127.0.0.1` isn't the
same `127.0.0.1` Docker forwards to. The image's `CMD` needs
`viva serve --host 0.0.0.0 --port 8000` explicitly.

**This interacts directly with Phase 15/18/20's existing auth work, in a
good way, not a gap to work around:** `_requires_auth("0.0.0.0")` is
`True` (`app.py:58-59`, `_LOOPBACK_HOSTS` is exactly
`{"127.0.0.1", "localhost", "::1"}` -- confirmed directly, `0.0.0.0`
isn't in it). So a containerized `viva serve` always requires a token,
correctly and automatically, with zero new code -- the existing
Phase 15 design already does the right thing here without this phase
needing to add anything. The one thing worth documenting clearly (§26.6):
the generated token prints to the container's stdout, so retrieving it
means `docker compose logs viva` (or `docker logs <container>`), not
something this phase needs to build a new mechanism for.

## 26.4 Two runtime requirements, confirmed directly, not assumed

**`git` isn't in `python:3.11-slim`.** `GitPython` shells out to a real
`git` binary (`git.Git().version()` confirmed this directly against this
sandbox's own git) -- `python:3.11-slim`'s Debian base doesn't include
`git` by default. Without an explicit `apt-get install -y git`, the
entire ingest/clone pipeline -- this project's core functionality --
would fail immediately inside the container. `ca-certificates` too, for
HTTPS clones against GitHub.

**`tree-sitter-language-pack` downloads grammars on first use, cached
under `~/.cache/tree-sitter-language-pack/`.** Confirmed directly, not
assumed from the CI workflow's own existing comment about this (Phase
19's `tests.yml`): this sandbox's cache already had exactly the four
languages (`go`, `javascript`, `python`, `rust`) its own test runs had
actually exercised, nothing more -- grammars aren't bundled upfront for
all 371 supported languages, they're fetched and cached lazily per
language on first real use. Two consequences for the container:

- **Runtime network access is needed**, not just build-time -- the first
  time someone's repo uses a language this particular container hasn't
  seen yet, that grammar downloads then. Not a new requirement this
  phase introduces (the CLI already needs this, natively installed or
  not) but worth stating plainly in the README's Docker section rather
  than leaving it as a silent assumption.
- **The cache directory needs a volume mount**, or every `docker compose
  up` re-downloads every grammar the person's repos actually use, on
  every restart. Decision: mount it as a named volume
  (`tree_sitter_cache:/root/.cache/tree-sitter-language-pack`), separate
  from the `data/` bind mount (§26.5) -- this one doesn't need to be
  human-inspectable on the host the way session data does, a Docker
  -managed named volume is the right tool, not a bind mount.

## 26.5 Container user: non-root, with a volume-mount consequence

Decision: the image runs as a non-root user, not root -- standard Docker
hardening practice, and consistent with this project's own security
posture across Phases 14/15/18 (constant-time comparisons, minimal
tokens, no more privilege than needed). Consequence worth being explicit
about: `$HOME` for that user won't be `/root`, so §26.4's cache-volume
mount path and any `$HOME`-relative path need to match whatever user the
`Dockerfile` actually creates -- decided at implementation time to match
the exact `useradd`/`HOME` the `Dockerfile` ends up with, not guessed at
here in the abstract.

## 26.6 `docker-compose.yml` design

```yaml
services:
  viva:
    build: .
    ports:
      - "8000:8000"
    environment:
      - LLM_MODEL=${LLM_MODEL}
      - EMBEDDING_MODEL=${EMBEDDING_MODEL:-nomic-embed-text}
      - OLLAMA_HOST=${OLLAMA_HOST:-http://host.docker.internal:11434}
      - GITHUB_TOKEN=${GITHUB_TOKEN:-}
    extra_hosts:
      - "host.docker.internal:host-gateway"  # no-op on Docker Desktop, needed on native Linux -- §26.2
    volumes:
      - ./data:/app/data
      - tree_sitter_cache:/home/viva/.cache/tree-sitter-language-pack  # exact path per §26.5

volumes:
  tree_sitter_cache:
```

`env_file: .env` is deliberately **not** used here in place of the
explicit `environment:` list above, even though `Config.load()` itself
reads `.env` via `load_dotenv()` -- inside the container, there's no
`.env` file on disk unless one is separately bind-mounted, and mounting
the person's real `.env` (which may carry a real `GITHUB_TOKEN`) into an
image just to have `load_dotenv()` read it back out is an unnecessary
extra file-sharing surface when Compose's own `environment:` +
host-shell `.env` (Compose itself reads a `.env` file in the project
directory for variable *substitution*, separately from `python-dotenv`)
already covers it with less surface area. One `.env` file, read by
Compose, substituted into `environment:` -- not two different `.env`
-reading mechanisms layered on each other.

`GITHUB_TOKEN` defaults to empty, not omitted -- `Config.load()` already
treats an unset/empty `GITHUB_TOKEN` as "no token, use unauthenticated
clone," so an empty default here is a real, intentional value, not a
placeholder.

## 26.7 `Dockerfile` design

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash viva
WORKDIR /app

COPY pyproject.toml requirements.txt ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Not --voice: native audio deps (PortAudio via sounddevice) assume a
# host audio device a headless container doesn't have -- same
# optional-by-design reasoning as Phase 11 itself, doubly true here.

USER viva
EXPOSE 8000

CMD ["viva", "serve", "--host", "0.0.0.0", "--port", "8000"]
```

`pip install .` (the `pyproject.toml`-driven path), not
`pip install -r requirements.txt` -- Phase 18 fixed `requirements.txt`
to actually work, but `pyproject.toml` remains the single source of
truth for this project's own dependency versions, and a fresh image
build is exactly the case with no reason to prefer the secondary
manifest over the primary one. `requirements.txt` is still copied in
(harmless, small) in case a future `pip install -r requirements.txt`
step is added for layer-caching reasons, but isn't used by this Dockerfile
as written.

A `.dockerignore` excluding `data/`, `cache/`, `.git/`, `tests/`,
`docs/`, `__pycache__/`, and `*.pyc` keeps the build context small and
avoids ever copying real session data into an image layer.

## 26.8 Documentation update plan

| File | Change |
|---|---|
| `Dockerfile`, `docker-compose.yml`, `.dockerignore` | new files, §26.6/§26.7 |
| `README.md` | new "Docker" subsection under Installation, alongside (not replacing) the existing native-install path -- covers `OLLAMA_HOST` per-platform (§26.2), retrieving the token via `docker compose logs` (§26.3), and the tree-sitter cache/network note (§26.4) |
| `CHANGELOG.md` | `[Unreleased]` entry once implemented and verified |
| `docs/plan.md` | Phase 21 entry gets its `**Verified**` line once real Docker validation (§26.9) actually happens -- more load-bearing than usual here, given this doc's own up-front caveat |

## 26.9 Test plan / exit criteria

Every item here needs real Docker, which this design doc's own
authoring environment doesn't have -- these are what `plan.md`'s
`**Verified**` line should actually confirm before being written, not a
formality:

- `docker compose build` succeeds.
- `docker compose up` with a real `OLLAMA_HOST` (native Linux and, if
  available, Docker Desktop) produces a `viva serve` reachable at
  `http://localhost:8000` from the host.
- `docker compose logs viva` shows the generated access token; a browser
  request to `http://localhost:8000?token=<that token>` reaches the app
  (§26.3's auth-token-in-container claim, confirmed live, not just
  reasoned about).
- A full session -- start, answer questions, view the report -- completed
  entirely through the containerized path against a real repo and a
  real (external) Ollama instance.
- `docker compose down && docker compose up` (container recreated, named
  volumes preserved): the prior session is still listed and its report
  still generates, proving the `data/` bind mount actually persists
  across recreation, not just across a simple restart.
- A repo using a language not yet in the `tree_sitter_cache` volume
  successfully triggers a grammar download from inside the running
  container (confirms §26.4's runtime-network-access claim is actually
  survivable, not just theorized), and a second `analyze`/`start` run
  against a repo in that same language, after a container restart,
  does **not** re-download it (confirms the volume mount actually works).

## 26.10 Deferred, not in scope for this phase

- **Pre-warming the `tree_sitter_cache` volume at build time** for a
  fixed set of "common" languages -- a real option, but adds build
  complexity and image size for a benefit that only helps until someone
  hits a language outside whatever fixed set was chosen, at which point
  §26.4's runtime-download path is needed anyway. Not worth it unless a
  real user reports first-use latency as an actual problem.
- **A GHCR/Docker Hub published image** -- this phase only produces a
  `Dockerfile` a person builds locally (`docker compose build`), not a
  publishing pipeline. A real follow-up, not this phase's scope.
- **Bundling Ollama itself** -- decided against in §26.1; revisit only if
  the external-Ollama friction turns out to be worse in practice than
  reasoned here, which real usage, not more reasoning, would need to show.
