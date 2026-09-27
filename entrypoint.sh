#!/bin/sh
# Runs as root (the Dockerfile deliberately does NOT `USER viva` itself --
# this script does the privilege drop, after fixing ownership first).
#
# Why this exists: `viva`'s own code creates ./logs/ on every single CLI
# invocation (cli.py's app callback, unconditional) and ./data/ holds the
# session/vector DBs (Config.session_db_path/vector_db_path). Both are
# relative to CWD (/app in this image). tree-sitter-language-pack's own
# grammar cache (~/.cache/tree-sitter-language-pack, downloaded on first
# use per language -- design doc §26.4) is the third path this needs to
# cover. /app itself is owned by root from the Dockerfile's COPY/pip
# install steps; /app/data is a *bind mount* from the host
# (docker-compose.yml's `./data:/app/data`), and Docker auto-creates a
# missing host-side bind-mount target as root-owned; the cache path is a
# named volume, whose first-use ownership isn't worth leaving to chance
# either. Any of the three, run as a non-root user with no fix, is a
# PermissionError on first `mkdir`/write -- the container would crash on
# startup before the web server ever binds. A build-time `chown` in the
# Dockerfile can't fix the bind-mount case: the mount's content (and its
# ownership) only exists at *runtime*, once docker-compose actually
# attaches the host directory -- whatever the image had baked in for that
# path gets replaced the moment the mount happens. Fixing ownership here,
# at container start, is the standard pattern for exactly this (the same
# one official postgres/mysql images use), not a one-off workaround.
set -e

mkdir -p /app/data /app/logs /home/viva/.cache/tree-sitter-language-pack
chown -R viva:viva /app/data /app/logs /home/viva/.cache

# Plain `su`, not `gosu` -- `gosu` isn't in a base Debian install and its
# availability via `apt-get install` couldn't be verified without a real
# `apt-get update` against Debian's repos, which this design doc's own
# authoring environment doesn't have network access to check (only a
# fixed allowlist of package/API hosts, not deb.debian.org). `su` is
# part of every Debian base image's essential `login` package -- no new
# dependency, nothing to get wrong. `-s /bin/sh -c 'exec "$0" "$@"' --
# "$@"` is the standard safe idiom for passing an argv array through
# `su -c` without word-splitting or quoting issues (`su -c` otherwise
# takes a single string, which would mangle arguments containing spaces
# -- not the case for this image's fixed CMD today, but no reason to
# rely on that staying true).
exec su viva -s /bin/sh -c 'exec "$0" "$@"' -- "$@"
