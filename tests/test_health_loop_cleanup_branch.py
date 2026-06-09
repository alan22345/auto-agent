"""Phase 3 — cleanup-branch lifecycle (real-git integration tests)."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from agent.health_loop import cleanup_branch
from agent.health_loop.cleanup_branch import (
    DEFAULT_CLEANUP_BRANCH,
    ensure_cleanup_branch,
)


@pytest.mark.asyncio
async def test_force_push_refuses_branch_not_in_allowlist():
    """The guardrail: force-pushing anything outside the allowlist raises
    BEFORE any git command runs."""
    from unittest.mock import AsyncMock, patch

    sh_run = AsyncMock()
    with (
        patch.object(cleanup_branch.sh, "run", sh_run),
        pytest.raises(ValueError, match="allowlist"),
    ):
        await cleanup_branch._force_push_cleanup(
            workspace="/tmp/x",
            cleanup_branch="main",  # NOT the cleanup branch
            allowed_branches={DEFAULT_CLEANUP_BRANCH},
        )
    sh_run.assert_not_called()  # never reached the push


def _run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def _set_identity(work):
    _run(["git", "config", "user.email", "test@auto-agent.local"], work)
    _run(["git", "config", "user.name", "auto-agent-test"], work)


@pytest.fixture
def git_repos(tmp_path):
    """A bare 'origin' + a working clone with a single commit on 'main'."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    _set_identity(work)
    (work / "README.md").write_text("base\n")
    _run(["git", "add", "."], work)
    _run(["git", "commit", "-m", "init"], work)
    _run(["git", "push", "-u", "origin", "main"], work)
    return SimpleNamespace(origin=str(origin), work=str(work))


def _current_branch(work: str) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _remote_has_branch(origin: str, branch: str) -> bool:
    out = subprocess.run(
        ["git", "ls-remote", "--heads", origin, branch],
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(out.stdout.strip())


@pytest.mark.asyncio
async def test_ensure_creates_cleanup_branch_off_base_when_missing(git_repos):
    await ensure_cleanup_branch(
        workspace=git_repos.work, base_branch="main", cleanup_branch=DEFAULT_CLEANUP_BRANCH
    )
    # Now checked out on the cleanup branch, and it exists on origin.
    assert _current_branch(git_repos.work) == DEFAULT_CLEANUP_BRANCH
    assert _remote_has_branch(git_repos.origin, DEFAULT_CLEANUP_BRANCH)


@pytest.mark.asyncio
async def test_ensure_is_idempotent_when_branch_already_exists(git_repos):
    await ensure_cleanup_branch(
        workspace=git_repos.work, base_branch="main", cleanup_branch=DEFAULT_CLEANUP_BRANCH
    )
    # Second call must not fail and must leave us on the cleanup branch.
    await ensure_cleanup_branch(
        workspace=git_repos.work, base_branch="main", cleanup_branch=DEFAULT_CLEANUP_BRANCH
    )
    assert _current_branch(git_repos.work) == DEFAULT_CLEANUP_BRANCH
