"""Phase 3 — cleanup-branch lifecycle (real-git integration tests)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.health_loop import cleanup_branch
from agent.health_loop.cleanup_branch import (
    DEFAULT_CLEANUP_BRANCH,
    RebaseOutcome,
    ensure_cleanup_branch,
    merge_fix,
    rebase_onto_base,
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


def _set_identity(work: Path):
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


def _commit_file(work: str, name: str, content: str, msg: str, branch: str | None = None):
    if branch is not None:
        _run(["git", "checkout", "-b", branch], work)
    (Path(work) / name).write_text(content)
    _run(["git", "add", "."], work)
    _run(["git", "commit", "-m", msg], work)


@pytest.mark.asyncio
async def test_merge_fix_brings_fix_commit_onto_cleanup(git_repos):
    work = git_repos.work
    await ensure_cleanup_branch(
        workspace=work, base_branch="main", cleanup_branch=DEFAULT_CLEANUP_BRANCH
    )
    # A fix branch off the cleanup tip adds a file.
    _commit_file(work, "fix.py", "print('fixed')\n", "health fix", branch="fix/dead-code")

    merged = await merge_fix(
        workspace=work, fix_branch="fix/dead-code", cleanup_branch=DEFAULT_CLEANUP_BRANCH
    )

    assert merged is True
    # The fix file is present on the cleanup branch and pushed to origin.
    _run(["git", "checkout", DEFAULT_CLEANUP_BRANCH], work)
    assert (Path(work) / "fix.py").exists()
    log = subprocess.run(
        ["git", "log", "--oneline", "origin/" + DEFAULT_CLEANUP_BRANCH],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "health fix" in log


def _advance_main(git_repos, name: str, content: str, msg: str):
    """Add a commit to main on origin (via a throwaway clone)."""
    work = git_repos.work
    _run(["git", "checkout", "main"], work)
    (Path(work) / name).write_text(content)
    _run(["git", "add", "."], work)
    _run(["git", "commit", "-m", msg], work)
    _run(["git", "push", "origin", "main"], work)


@pytest.mark.asyncio
async def test_rebase_onto_base_replays_cleanup_on_advanced_main(git_repos):
    work = git_repos.work
    await ensure_cleanup_branch(
        workspace=work, base_branch="main", cleanup_branch=DEFAULT_CLEANUP_BRANCH
    )
    # After ensure_cleanup_branch we are on the cleanup branch. Put a
    # non-conflicting fix on it (new file) and push.
    (Path(work) / "fix.py").write_text("print('fixed')\n")
    _run(["git", "add", "."], work)
    _run(["git", "commit", "-m", "health fix"], work)
    _run(["git", "push", "origin", DEFAULT_CLEANUP_BRANCH], work)
    # main advances with a DIFFERENT file (no conflict).
    _advance_main(git_repos, "main.py", "print('main')\n", "advance main")

    outcome = await rebase_onto_base(
        workspace=work,
        base_branch="main",
        cleanup_branch=DEFAULT_CLEANUP_BRANCH,
        allowed_branches={DEFAULT_CLEANUP_BRANCH},
    )

    assert isinstance(outcome, RebaseOutcome)
    assert outcome.ok is True and outcome.conflict is False
    # Cleanup now contains BOTH main's new file and the fix, on origin.
    _run(["git", "fetch", "origin"], work)
    files = subprocess.run(
        ["git", "ls-tree", "--name-only", "origin/" + DEFAULT_CLEANUP_BRANCH],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "fix.py" in files and "main.py" in files


@pytest.mark.asyncio
async def test_rebase_conflict_aborts_and_reports(git_repos):
    work = git_repos.work
    await ensure_cleanup_branch(
        workspace=work, base_branch="main", cleanup_branch=DEFAULT_CLEANUP_BRANCH
    )
    # Cleanup edits README.
    (Path(work) / "README.md").write_text("cleanup change\n")
    _run(["git", "add", "."], work)
    _run(["git", "commit", "-m", "cleanup edits README"], work)
    _run(["git", "push", "origin", DEFAULT_CLEANUP_BRANCH], work)
    # main edits the SAME line ⇒ rebase conflict.
    _advance_main(git_repos, "README.md", "main change\n", "main edits README")

    outcome = await rebase_onto_base(
        workspace=work,
        base_branch="main",
        cleanup_branch=DEFAULT_CLEANUP_BRANCH,
        allowed_branches={DEFAULT_CLEANUP_BRANCH},
    )

    assert outcome.ok is False and outcome.conflict is True
    # The repo is not mid-rebase (abort cleaned up).
    status = subprocess.run(["git", "status"], cwd=work, capture_output=True, text=True).stdout
    assert "rebase in progress" not in status.lower()


@pytest.mark.asyncio
async def test_merge_fix_conflict_aborts_and_returns_false(git_repos):
    work = git_repos.work
    await ensure_cleanup_branch(
        workspace=work, base_branch="main", cleanup_branch=DEFAULT_CLEANUP_BRANCH
    )
    # Cleanup edits the only line of README and pushes.
    (Path(work) / "README.md").write_text("cleanup line\n")
    _run(["git", "add", "."], work)
    _run(["git", "commit", "-m", "cleanup edits README"], work)
    _run(["git", "push", "origin", DEFAULT_CLEANUP_BRANCH], work)
    # A fix branch off main edits the SAME line to a different value, so it
    # genuinely diverges from cleanup ⇒ merge --no-ff must conflict.
    _run(["git", "checkout", "-b", "fix/conflict", "main"], work)
    (Path(work) / "README.md").write_text("fix line\n")
    _run(["git", "add", "."], work)
    _run(["git", "commit", "-m", "fix edits README"], work)

    merged = await merge_fix(
        workspace=work, fix_branch="fix/conflict", cleanup_branch=DEFAULT_CLEANUP_BRANCH
    )

    assert merged is False
    # No dangling merge state: abort cleaned up MERGE_HEAD and unmerged paths.
    assert not (Path(work) / ".git" / "MERGE_HEAD").exists()
    status = subprocess.run(["git", "status"], cwd=work, capture_output=True, text=True).stdout
    assert "unmerged" not in status.lower()
