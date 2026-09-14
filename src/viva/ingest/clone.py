"""Repo cloning + commit-SHA resolution (FR1).

Uses GitPython rather than shelling out to `git` via subprocess directly:
it's easier to unit-test (mock the `Repo` object instead of parsing CLI
stdout) and gives clean commit-SHA / branch access without scraping
output. Private-repo auth is done by embedding `GITHUB_TOKEN` into the
HTTPS clone URL rather than a credential helper -- the simplest thing
that works non-interactively, and it matches config.py's existing note
(see docs/plan.md Phase 1) that an invalid token should surface via
GitHub's own auth error rather than being pre-validated in this codebase.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from git import GitCommandError, Repo


class CloneError(RuntimeError):
    """Raised when a repo can't be cloned (bad URL, auth failure, network)."""


@dataclass(frozen=True)
class ClonedRepo:
    repo_url: str
    repo_slug: str
    branch: str
    commit_sha: str
    local_path: Path


_ALLOWED_URL_SCHEMES = frozenset({"https", "ssh"})
_URL_PATH_SLUG_PATTERN = re.compile(r"^/(?P<owner>[^/]+)/(?P<name>[^/.]+?)(?:\.git)?/?$")
_SCP_LIKE_PATTERN = re.compile(r"^[\w.-]+@github\.com:(?P<owner>[^/]+)/(?P<name>[^/.]+?)(?:\.git)?/?$")


def validate_repo_url(repo_url: str) -> str:
    """Validate `repo_url` is a github.com URL and return its owner/repo
    slug, in one pass.

    Replaces the old `_repo_slug()`, which validated the raw string with
    a tail-anchored regex (`re.search()`, anchored only at the end) while
    `_with_token()` separately parsed the same string with `urlsplit()`
    -- the two could disagree about the URL's actual host. A URL like
    `https://attacker.example/x/github.com/owner/repo` satisfied the old
    regex (matched at the string's tail) while `urlsplit()` resolved its
    host to `attacker.example`, letting `GITHUB_TOKEN` be attached to the
    wrong host. See docs/system-design/19-panel-review-findings-2026-09.md
    §19.4.2/§19.4.3 and docs/system-design/20-phase-14-security-
    hardening-design.md §20.1 for the full writeup.

    Three accepted shapes, matching what `_with_token()` already assumes
    elsewhere in this module:
    - `https://github.com/owner/repo(.git)?`
    - `ssh://git@github.com/owner/repo(.git)?`
    - `git@github.com:owner/repo(.git)?` (SCP-like syntax -- no scheme, so
      it's checked separately before `urlsplit()` is consulted at all;
      `urlsplit()` doesn't parse this form as having a host).

    Raises CloneError for anything else, including a scheme outside
    {https, ssh} (closes §19.4.3 -- nothing reaches `Repo.clone_from()`
    with an exotic transport like `ext::`) and a parsed host that isn't
    exactly `github.com` (closes §19.4.2 -- a URL that merely contains
    the substring "github.com" no longer passes).
    """
    scp_match = _SCP_LIKE_PATTERN.match(repo_url)
    if scp_match:
        return f"{scp_match.group('owner')}/{scp_match.group('name')}"

    parts = urlsplit(repo_url)
    if parts.scheme in _ALLOWED_URL_SCHEMES and parts.hostname == "github.com":
        path_match = _URL_PATH_SLUG_PATTERN.match(parts.path)
        if path_match:
            return f"{path_match.group('owner')}/{path_match.group('name')}"

    raise CloneError(
        f"Could not derive a github.com owner/repo slug from {repo_url!r}; "
        "expected https://github.com/owner/repo, "
        "ssh://git@github.com/owner/repo, or git@github.com:owner/repo."
    )


def _with_token(repo_url: str, github_token: str | None) -> str:
    if not github_token:
        return repo_url
    parts = urlsplit(repo_url)
    if parts.scheme != "https" or parts.hostname != "github.com":
        # SSH/SCP URLs carry their own auth -- token injection only makes
        # sense for HTTPS clone URLs. The host check is independent of
        # call order (defense in depth per §20.1.2): even if a future
        # caller reaches this without going through validate_repo_url()
        # first, a token is never attached to a host that isn't exactly
        # github.com.
        return repo_url
    netloc = f"{github_token}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def clone_repo(
    repo_url: str,
    dest_dir: Path,
    branch: str | None = None,
    github_token: str | None = None,
) -> ClonedRepo:
    """Clone `repo_url` into `dest_dir` and pin its current commit SHA.

    Shallow (`depth=1`) -- nothing downstream reads git history, only the
    current tree state and its commit SHA (for Chroma collection keying,
    05-repo-lifecycle-and-language-coverage.md §5.2) matter.
    """
    repo_slug = validate_repo_url(repo_url)
    clone_url = _with_token(repo_url, github_token)

    clone_kwargs: dict[str, object] = {"depth": 1}
    if branch:
        clone_kwargs["branch"] = branch

    try:
        repo = Repo.clone_from(clone_url, dest_dir, **clone_kwargs)
    except GitCommandError as exc:
        branch_note = f" (branch {branch!r})" if branch else ""
        raise CloneError(f"Failed to clone {repo_url!r}{branch_note}: {exc}") from exc

    resolved_branch = branch or repo.active_branch.name
    commit_sha = repo.head.commit.hexsha[:12]

    return ClonedRepo(
        repo_url=repo_url,
        repo_slug=repo_slug,
        branch=resolved_branch,
        commit_sha=commit_sha,
        local_path=dest_dir,
    )
