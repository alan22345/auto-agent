"""Workspace management — clone repos and manage task branches.

Copied from claude_runner/workspace.py with no CLI dependency.
"""

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
        # Include the failed git args in the error for easier diagnosis
        raise RuntimeError(
            f"git {args[0]} failed: {stderr_str.strip() or stdout_str.strip()}"
        )
    return stdout_str, stderr_str, proc.returncode


# Identity used for auto-commits by the safety net, and configured in each
# cloned workspace so the agent's own commits don't fail on fresh containers.
_AGENT_GIT_NAME = "auto-agent"
_AGENT_GIT_EMAIL = "auto-agent@bot.local"


async def _remote_branch_exists(repo_url: str, branch: str) -> bool:
    """Check whether `branch` exists on the remote. Uses `git ls-remote`."""
    out, _, rc = await _run_git("ls-remote", "--heads", repo_url, branch)
    return rc == 0 and bool(out.strip())


async def clone_repo(
    repo_url: str,
    task_id: int,
    default_branch: str = "main",
    workspace_name: str | None = None,
    fallback_branch: str | None = None,
) -> str:
    """Clone a repo into an isolated workspace directory. Returns the workspace path.

    If `default_branch` doesn't exist on the remote and `fallback_branch` is
    provided, the repo is cloned at `fallback_branch`, then `default_branch`
    is created locally from it and pushed upstream. This supports freeform
    configs where the dev branch hasn't been created yet — the orchestrator
    creates it on first use rather than failing.

    If the workspace already exists (from a previous phase of the same task),
    it is reused and pulled to get latest changes instead of re-cloning.

    Args:
        workspace_name: Override the workspace directory name. If not provided,
            defaults to "task-{task_id}".
        fallback_branch: If `default_branch` doesn't exist on the remote, clone
            this one and create `default_branch` from it.
    """
    dirname = workspace_name or f"task-{task_id}"
    workspace = os.path.join(WORKSPACES_DIR, dirname)

    # Inject GitHub token into URL for auth (used by both reuse and fresh paths)
    authed_url = repo_url
    if settings.github_token and "github.com" in authed_url:
        authed_url = authed_url.replace(
            "https://github.com",
            f"https://{settings.github_token}@github.com",
        )

    if os.path.exists(workspace):
        # Reuse existing workspace — make sure we have the latest default branch
        await _run_git("fetch", "origin", default_branch, cwd=workspace)
        await _run_git("checkout", default_branch, cwd=workspace)
        await _run_git("reset", "--hard", f"origin/{default_branch}", cwd=workspace)
        # Re-assert git identity (local config could have been blown away)
        await _run_git("config", "user.email", _AGENT_GIT_EMAIL, cwd=workspace)
        await _run_git("config", "user.name", _AGENT_GIT_NAME, cwd=workspace)
        return workspace

    # Check if the requested branch actually exists on the remote.
    # If not and we have a fallback, clone that then create the missing branch.
    if fallback_branch and not await _remote_branch_exists(authed_url, default_branch):
        import structlog
        log = structlog.get_logger()
        log.info(
            "dev_branch_missing_creating_from_fallback",
            missing_branch=default_branch,
            fallback=fallback_branch,
        )
        await _run_git("clone", "-b", fallback_branch, authed_url, workspace, check=True)
        await _run_git("config", "user.email", _AGENT_GIT_EMAIL, cwd=workspace)
        await _run_git("config", "user.name", _AGENT_GIT_NAME, cwd=workspace)
        await _run_git("checkout", "-b", default_branch, cwd=workspace, check=True)
        await _run_git("push", "-u", "origin", default_branch, cwd=workspace, check=True)
        return workspace

    await _run_git("clone", "-b", default_branch, authed_url, workspace, check=True)

    # Configure local git identity so the agent's commits work in containers
    # that have no global gitconfig. Local config overrides nothing upstream
    # but satisfies `git commit`'s identity requirement.
    await _run_git("config", "user.email", _AGENT_GIT_EMAIL, cwd=workspace)
    await _run_git("config", "user.name", _AGENT_GIT_NAME, cwd=workspace)

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


class EmptyBranchError(RuntimeError):
    """Raised when a task's branch has no commits relative to its base branch.

    This happens when the agent writes code but never runs `git commit`,
    leaving the branch pointing at base. Pushing and attempting to create
    a PR would fail with a misleading error from GitHub; we catch it here
    with a clear message instead. See task 48 post-mortem.
    """


async def commit_pending_changes(workspace: str, task_id: int, title: str) -> bool:
    """Auto-commit any uncommitted changes in the workspace.

    Safety net for agents that forget to run `git commit`. Stages all
    tracked modifications AND untracked files, then commits them with a
    task-descriptive message.

    Returns True if a commit was made, False if there was nothing to commit.
    """
    # `git status --porcelain` lists both unstaged + untracked files
    status, _, _ = await _run_git("status", "--porcelain", cwd=workspace)
    if not status.strip():
        return False

    await _run_git("add", "-A", cwd=workspace, check=True)

    # Re-check — `git add` might have turned out to be a no-op if everything
    # was only in gitignored paths
    staged, _, _ = await _run_git("diff", "--cached", "--name-only", cwd=workspace)
    if not staged.strip():
        return False

    safe_title = title.replace("\n", " ").strip()[:72]
    message = f"Task #{task_id}: {safe_title}\n\nAuto-committed by auto-agent safety net."
    # Pass identity via -c flags so this works regardless of global/local git config
    await _run_git(
        "-c", f"user.email={_AGENT_GIT_EMAIL}",
        "-c", f"user.name={_AGENT_GIT_NAME}",
        "commit", "-m", message,
        cwd=workspace, check=True,
    )
    return True


async def ensure_branch_has_commits(workspace: str, base_branch: str) -> None:
    """Verify the current branch has at least one commit relative to base.

    Raises EmptyBranchError if the branch's HEAD is the same as `base_branch`'s
    HEAD (no new work to PR).
    """
    log_output, _, returncode = await _run_git(
        "log", f"{base_branch}..HEAD", "--oneline", cwd=workspace,
    )
    if returncode != 0:
        # The base branch may not exist locally; fetch and retry once
        await _run_git("fetch", "origin", base_branch, cwd=workspace)
        log_output, _, returncode = await _run_git(
            "log", f"origin/{base_branch}..HEAD", "--oneline", cwd=workspace,
        )
        if returncode != 0:
            # If we still can't resolve the base, surface a clear error
            raise EmptyBranchError(
                f"Could not verify commits against base '{base_branch}'. "
                f"The base branch may not exist or is unreachable."
            )

    if not log_output.strip():
        raise EmptyBranchError(
            f"Branch has no commits relative to '{base_branch}'. "
            f"The agent likely wrote code but forgot to commit it — or made no changes at all."
        )


def cleanup_workspace(task_id: int) -> None:
    """Remove a task's workspace."""
    workspace = os.path.join(WORKSPACES_DIR, f"task-{task_id}")
    if os.path.exists(workspace):
        shutil.rmtree(workspace)
