"""Workspace management — clone repos and manage task branches."""

from __future__ import annotations

import asyncio
import os
import shutil

from shared.config import settings

WORKSPACES_DIR = os.environ.get("WORKSPACES_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), ".workspaces"))


async def _run_git(*args: str, cwd: str | None = None, check: bool = False) -> tuple[str, str, int]:
    """Run a git command asynchronously. Returns (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    stdout_str = (stdout or b"").decode()
    stderr_str = (stderr or b"").decode()
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {stderr_str.strip() or stdout_str.strip()}")
    return stdout_str, stderr_str, proc.returncode


async def clone_repo(repo_url: str, task_id: int, default_branch: str = "main", workspace_name: str | None = None) -> str:
    """Clone a repo into an isolated workspace directory. Returns the workspace path.

    If the workspace already exists (from a previous phase of the same task),
    it is reused and pulled to get latest changes instead of re-cloning.

    Args:
        workspace_name: Override the workspace directory name. If not provided,
            defaults to "task-{task_id}".
    """
    dirname = workspace_name or f"task-{task_id}"
    workspace = os.path.join(WORKSPACES_DIR, dirname)
    if os.path.exists(workspace):
        # Reuse existing workspace — make sure we have the latest default branch
        await _run_git("fetch", "origin", default_branch, cwd=workspace)
        await _run_git("checkout", default_branch, cwd=workspace)
        await _run_git("reset", "--hard", f"origin/{default_branch}", cwd=workspace)
        return workspace

    # Inject GitHub token into URL for auth
    if settings.github_token and "github.com" in repo_url:
        repo_url = repo_url.replace(
            "https://github.com",
            f"https://{settings.github_token}@github.com",
        )

    await _run_git("clone", "-b", default_branch, repo_url, workspace, check=True)
    return workspace


async def create_branch(workspace: str, branch_name: str) -> None:
    """Create and checkout a branch, reusing it if it already exists."""
    _, _, returncode = await _run_git("rev-parse", "--verify", branch_name, cwd=workspace)
    if returncode == 0:
        await _run_git("checkout", branch_name, cwd=workspace, check=True)
    else:
        await _run_git("checkout", "-b", branch_name, cwd=workspace, check=True)


async def push_branch(workspace: str, branch_name: str) -> None:
    """Push the branch to remote."""
    await _run_git("push", "-u", "origin", branch_name, cwd=workspace, check=True)


def cleanup_workspace(task_id: int) -> None:
    """Remove a task's workspace."""
    workspace = os.path.join(WORKSPACES_DIR, f"task-{task_id}")
    if os.path.exists(workspace):
        shutil.rmtree(workspace)
