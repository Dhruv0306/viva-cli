"""Tests for viva.ingest.clone.

GitPython's `Repo.clone_from` is mocked throughout -- no real network
calls. This exercises URL parsing, token injection, and error handling in
isolation; the actual network path is covered separately by an
opt-in, network-marked integration test (see test_ingest_integration.py).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from git import GitCommandError

from viva.ingest.clone import CloneError, _repo_slug, _with_token, clone_repo


def test_repo_slug_from_https_url() -> None:
    assert _repo_slug("https://github.com/Dhruv0306/viva-cli") == "Dhruv0306/viva-cli"


def test_repo_slug_from_https_url_with_dot_git_suffix() -> None:
    assert _repo_slug("https://github.com/Dhruv0306/viva-cli.git") == "Dhruv0306/viva-cli"


def test_repo_slug_from_ssh_url() -> None:
    assert _repo_slug("git@github.com:Dhruv0306/viva-cli.git") == "Dhruv0306/viva-cli"


def test_repo_slug_rejects_non_github_url() -> None:
    with pytest.raises(CloneError):
        _repo_slug("https://gitlab.com/someone/somewhere")


def test_with_token_injects_into_https_url() -> None:
    result = _with_token("https://github.com/owner/repo", "ghp_abc123")
    assert result == "https://ghp_abc123@github.com/owner/repo"


def test_with_token_leaves_ssh_url_untouched() -> None:
    url = "git@github.com:owner/repo.git"
    assert _with_token(url, "ghp_abc123") == url


def test_with_token_noop_when_no_token() -> None:
    url = "https://github.com/owner/repo"
    assert _with_token(url, None) == url


def test_clone_repo_returns_cloned_repo_on_success(tmp_path: Path, mocker) -> None:
    mock_repo = MagicMock()
    mock_repo.active_branch.name = "main"
    mock_repo.head.commit.hexsha = "abcdef0123456789"
    mock_clone_from = mocker.patch("viva.ingest.clone.Repo.clone_from", return_value=mock_repo)

    dest = tmp_path / "clone"
    result = clone_repo("https://github.com/owner/repo", dest)

    assert result.repo_slug == "owner/repo"
    assert result.branch == "main"
    assert result.commit_sha == "abcdef012345"  # truncated to 12 chars
    assert result.local_path == dest
    mock_clone_from.assert_called_once()
    assert mock_clone_from.call_args.kwargs.get("depth") == 1


def test_clone_repo_uses_explicit_branch(tmp_path: Path, mocker) -> None:
    mock_repo = MagicMock()
    mock_repo.head.commit.hexsha = "abcdef0123456789"
    mocker.patch("viva.ingest.clone.Repo.clone_from", return_value=mock_repo)

    result = clone_repo("https://github.com/owner/repo", tmp_path / "clone", branch="develop")

    assert result.branch == "develop"


def test_clone_repo_wraps_git_command_error(tmp_path: Path, mocker) -> None:
    mocker.patch(
        "viva.ingest.clone.Repo.clone_from",
        side_effect=GitCommandError("clone", 128, stderr="Authentication failed"),
    )

    with pytest.raises(CloneError, match="Failed to clone"):
        clone_repo("https://github.com/owner/private-repo", tmp_path / "clone")


def test_clone_repo_injects_token_into_clone_url(tmp_path: Path, mocker) -> None:
    mock_repo = MagicMock()
    mock_repo.active_branch.name = "main"
    mock_repo.head.commit.hexsha = "abcdef0123456789"
    mock_clone_from = mocker.patch("viva.ingest.clone.Repo.clone_from", return_value=mock_repo)

    clone_repo("https://github.com/owner/repo", tmp_path / "clone", github_token="ghp_secret")

    called_url = mock_clone_from.call_args.args[0]
    assert called_url == "https://ghp_secret@github.com/owner/repo"
