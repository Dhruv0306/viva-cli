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

from viva.ingest.clone import CloneError, _with_token, clone_repo, validate_repo_url


def test_validate_repo_url_from_https_url() -> None:
    assert validate_repo_url("https://github.com/Dhruv0306/viva-cli") == "Dhruv0306/viva-cli"


def test_validate_repo_url_from_https_url_with_dot_git_suffix() -> None:
    assert validate_repo_url("https://github.com/Dhruv0306/viva-cli.git") == "Dhruv0306/viva-cli"


def test_validate_repo_url_from_ssh_scheme_url() -> None:
    assert validate_repo_url("ssh://git@github.com/Dhruv0306/viva-cli.git") == "Dhruv0306/viva-cli"


def test_validate_repo_url_from_scp_like_url() -> None:
    assert validate_repo_url("git@github.com:Dhruv0306/viva-cli.git") == "Dhruv0306/viva-cli"


def test_validate_repo_url_rejects_non_github_url() -> None:
    with pytest.raises(CloneError):
        validate_repo_url("https://gitlab.com/someone/somewhere")


def test_validate_repo_url_rejects_host_bypass_via_trailing_slug() -> None:
    # docs/system-design/19-panel-review-findings-2026-09.md §19.4.2 --
    # the old tail-anchored regex matched this because "github.com/owner/
    # repo" appears at the end of the string, even though the URL's real
    # host (per urlsplit) is attacker.example, not github.com.
    with pytest.raises(CloneError):
        validate_repo_url("https://attacker.example/x/github.com/owner/repo")


def test_validate_repo_url_rejects_subdomain_suffix_spoof() -> None:
    with pytest.raises(CloneError):
        validate_repo_url("https://github.com.evil.com/owner/repo")


def test_validate_repo_url_rejects_disallowed_scheme() -> None:
    # docs/system-design/19-panel-review-findings-2026-09.md §19.4.3 --
    # git's `ext::` transport can run an arbitrary local command; nothing
    # outside {https, ssh} should ever reach Repo.clone_from().
    with pytest.raises(CloneError):
        validate_repo_url("ext::sh -c 'id' github.com/owner/repo")


def test_validate_repo_url_rejects_file_scheme() -> None:
    with pytest.raises(CloneError):
        validate_repo_url("file:///etc/passwd")


def test_validate_repo_url_rejects_malformed_path() -> None:
    with pytest.raises(CloneError):
        validate_repo_url("https://github.com/owner-only")


def test_with_token_injects_into_https_url() -> None:
    result = _with_token("https://github.com/owner/repo", "ghp_abc123")
    assert result == "https://ghp_abc123@github.com/owner/repo"


def test_with_token_leaves_ssh_url_untouched() -> None:
    url = "git@github.com:owner/repo.git"
    assert _with_token(url, "ghp_abc123") == url


def test_with_token_noop_when_no_token() -> None:
    url = "https://github.com/owner/repo"
    assert _with_token(url, None) == url


def test_with_token_refuses_non_github_host_even_called_directly() -> None:
    # Defense in depth per docs/system-design/20-phase-14-security-
    # hardening-design.md §20.1.2 -- independent of validate_repo_url()
    # having already run, _with_token() never attaches a token to a host
    # that isn't exactly github.com.
    result = _with_token("https://attacker.example/owner/repo", "ghp_secret")
    assert "ghp_secret" not in result
    assert result == "https://attacker.example/owner/repo"


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


def test_clone_repo_rejects_disallowed_scheme_before_touching_git(tmp_path: Path, mocker) -> None:
    # docs/system-design/19-panel-review-findings-2026-09.md §19.4.3 --
    # the only way to confirm an exotic transport (e.g. git's `ext::`,
    # which can run an arbitrary local command) never runs is to assert
    # Repo.clone_from() itself is never called.
    mock_clone_from = mocker.patch("viva.ingest.clone.Repo.clone_from")

    with pytest.raises(CloneError):
        clone_repo("ext::sh -c 'id' github.com/owner/repo", tmp_path / "clone")

    mock_clone_from.assert_not_called()


def test_clone_repo_never_leaks_token_to_bypass_host(tmp_path: Path, mocker) -> None:
    # docs/system-design/19-panel-review-findings-2026-09.md §19.4.2 --
    # the exact bypass shape that used to satisfy the old tail-anchored
    # slug regex while resolving to a different host under urlsplit().
    mock_clone_from = mocker.patch("viva.ingest.clone.Repo.clone_from")

    with pytest.raises(CloneError):
        clone_repo(
            "https://attacker.example/x/github.com/owner/repo",
            tmp_path / "clone",
            github_token="ghp_secret",
        )

    mock_clone_from.assert_not_called()
