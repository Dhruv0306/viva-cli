# Phase 21 (docs/plan.md, docs/system-design/
# 26-phase-21-containerized-setup-design.md). viva serve only -- no
# Ollama bundled, no GPU passthrough for this image to own (§26.1). Point
# OLLAMA_HOST at wherever you already run Ollama; see the README's
# "Docker" section for per-platform host networking.
FROM python:3.11-slim

# git: GitPython shells out to a real git binary (confirmed directly,
# `git.Git().version()`), which python:3.11-slim's Debian base does not
# include by default -- without this, the entire ingest/clone pipeline
# (this project's core functionality) fails immediately.
# ca-certificates: HTTPS clones against GitHub.
# No extra package for entrypoint.sh's privilege drop -- it uses plain
# `su` (part of every Debian base image already), not `gosu`, precisely
# to avoid depending on an apt package this design doc's own authoring
# environment couldn't verify is available (entrypoint.sh has the full
# reasoning).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash viva
WORKDIR /app

COPY pyproject.toml requirements.txt README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Not --voice: native audio deps (PortAudio via sounddevice) assume a
# host audio device a headless container doesn't have -- same
# optional-by-design reasoning as Phase 11 itself, doubly true here.

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Deliberately no `USER viva` here -- entrypoint.sh needs to start as
# root to fix ownership of the runtime-writable, bind-mounted ./data and
# the ./logs directory viva's own code creates on every invocation,
# *then* drops to the non-root `viva` user via `su` before exec'ing the
# real command. A build-time chown here can't do this: /app/data's
# actual content and ownership only exist once docker-compose's bind
# mount attaches at runtime, replacing whatever this image had baked in
# for that path.

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["viva", "serve", "--host", "0.0.0.0", "--port", "8000"]
