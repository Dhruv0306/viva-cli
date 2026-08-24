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


_SLUG_PATTERN = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<name>[^/.]+?)(?:\.git)?/?$")


def _repo_slug(repo_url: str) -> str:
    match = _SLUG_PATTERN.search(repo_url)
    if not match:
        raise CloneError(
            f"Could not derive a repo slug from {repo_url!r}; expected a "
            "github.com owner/repo URL."
        )
    return f"{match.group('owner')}/{match.group('name')}"


def _with_token(repo_url: str, github_token: str | None) -> str:
    if not github_token:
        return repo_url
    parts = urlsplit(repo_url)
    if parts.scheme not in ("http", "https"):
        # SSH URLs (git@github.com:...) already carry their own auth --
        # token injection only makes sense for HTTPS clone URLs.
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
    repo_slug = _repo_slug(repo_url)
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
