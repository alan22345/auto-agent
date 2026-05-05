"""Regression test for `clone_repo` reusing a stale workspace dir.

`cleanup_workspace` on a failed task (or a crashed prior run) can leave
`<WORKSPACES_DIR>/task-{id}/` on disk as either an empty directory or a
directory that no longer holds a valid git repo. The reuse path then
runs `git fetch origin <branch>` inside a non-repo and crashes — and
the crash happens *before* `handle_coding`'s try/except, so the task
gets stuck in CODING forever (no transition to FAILED).

This bit task #156 on the VM. Fix: treat "exists but no .git" as a
fresh-clone case, not a reuse case.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# clone_repo calls _run_git via agent.sh.run; we mock _run_git directly
# so the test doesn't need a real remote.
from agent import workspace


@pytest.mark.asyncio
async def test_clone_repo_reclones_when_dir_exists_but_not_a_git_repo(tmp_path):
    """Empty/non-git stale dir must trigger a fresh clone, not a fetch."""
    stale = tmp_path / "task-156"
    stale.mkdir()  # exists but has no .git — leftover from cleanup_workspace

    # Pretend the remote branch exists so the no-fallback path is taken.
    calls: list[tuple[str, ...]] = []

    async def fake_run_git(*args, cwd=None, check=False):
        calls.append(args)
        # `clone` is the call we expect to see; everything else returns success.
        return ("", "", 0)

    async def fake_remote_branch_exists(repo_url, branch):
        return True

    with patch.object(workspace, "WORKSPACES_DIR", str(tmp_path)), \
         patch.object(workspace, "_run_git", side_effect=fake_run_git), \
         patch.object(
             workspace, "_remote_branch_exists",
             side_effect=fake_remote_branch_exists,
         ):
        result = await workspace.clone_repo(
            "https://github.com/example/repo.git",
            task_id=156,
            default_branch="prod",
        )

    assert result == str(stale)
    # The fetch/checkout/reset triplet from the reuse path is the bug — none
    # of those should run when there's no .git.
    git_subcommands = [args[0] for args in calls if args]
    assert "fetch" not in git_subcommands, (
        "stale empty dir should NOT take the reuse-and-fetch path: "
        f"git calls = {calls}"
    )
    assert "clone" in git_subcommands, (
        f"stale empty dir must trigger a fresh clone; git calls = {calls}"
    )


@pytest.mark.asyncio
async def test_clone_repo_reuses_when_dir_has_git_metadata(tmp_path):
    """Sanity: a valid checkout (with .git) still takes the reuse path."""
    live = tmp_path / "task-200"
    live.mkdir()
    (live / ".git").mkdir()

    calls: list[tuple[str, ...]] = []

    async def fake_run_git(*args, cwd=None, check=False):
        calls.append(args)
        return ("", "", 0)

    with patch.object(workspace, "WORKSPACES_DIR", str(tmp_path)), \
         patch.object(workspace, "_run_git", side_effect=fake_run_git):
        await workspace.clone_repo(
            "https://github.com/example/repo.git",
            task_id=200,
            default_branch="main",
        )

    git_subcommands = [args[0] for args in calls if args]
    # Reuse path: fetch + checkout + reset, then identity config.
    assert "fetch" in git_subcommands
    assert "clone" not in git_subcommands
