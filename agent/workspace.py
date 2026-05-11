"""Workspace management — clone repos and manage task branches."""

from __future__ import annotations

import os
import shutil

from agent import sh

WORKSPACES_DIR = os.environ.get("WORKSPACES_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), ".workspaces"))


def _workspace_path(*, task_id: int, organization_id: int | None) -> str:
    """Resolve the workspace directory for a task.

    With organization_id set: `<WORKSPACES_DIR>/<org_id>/task-<task_id>` —
    per-org sub-tree so cross-org clones don't collide on repo names and
    `du -sh <WORKSPACES_DIR>/*` gives a per-org disk footprint.

    Without (legacy callers): `<WORKSPACES_DIR>/task-<task_id>`.
    """
    if organization_id is not None:
        return os.path.join(WORKSPACES_DIR, str(organization_id), f"task-{task_id}")
    return os.path.join(WORKSPACES_DIR, f"task-{task_id}")


async def _run_git(*args: str, cwd: str | None = None, check: bool = False) -> tuple[str, str, int | None]:
    """Run a git command asynchronously. Returns (stdout, stderr, returncode).

    Routes through ``agent.sh.run`` so this call inherits the seam's
    invariants — a 60s default timeout (no remote git command should hang
    indefinitely waiting for a TTY) and ``GIT_TERMINAL_PROMPT=0``.
    """
    result = await sh.run(["git", *args], cwd=cwd, timeout=60)
    if check and result.failed:
        # Include the failed git args in the error for easier diagnosis
        raise RuntimeError(
            f"git {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout, result.stderr, result.returncode


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
    *,
    user_id: int | None = None,
    organization_id: int | None = None,
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
    if workspace_name:
        workspace = os.path.join(WORKSPACES_DIR, workspace_name)
    else:
        workspace = _workspace_path(task_id=task_id, organization_id=organization_id)

    # Inject GitHub token into URL for auth (used by both reuse and fresh paths).
    # Both PAT and GitHub App installation tokens accept the
    # ``x-access-token:<token>@github.com`` form — App tokens *require* the
    # username, PATs are happy either way.
    from shared.github_auth import get_github_token

    gh_token = await get_github_token(user_id=user_id, organization_id=organization_id)
    authed_url = repo_url
    if gh_token and "github.com" in authed_url:
        authed_url = authed_url.replace(
            "https://github.com",
            f"https://x-access-token:{gh_token}@github.com",
        )

    if os.path.isdir(os.path.join(workspace, ".git")):
        # Reuse existing workspace — make sure we have the latest default branch
        await _run_git("fetch", "origin", default_branch, cwd=workspace)
        await _run_git("checkout", default_branch, cwd=workspace)
        await _run_git("reset", "--hard", f"origin/{default_branch}", cwd=workspace)
        # Re-assert git identity (local config could have been blown away)
        await _run_git("config", "user.email", _AGENT_GIT_EMAIL, cwd=workspace)
        await _run_git("config", "user.name", _AGENT_GIT_NAME, cwd=workspace)
        return workspace

    # Dir exists but is not a git checkout (e.g. cleanup_workspace left an
    # empty shell behind, or a prior clone crashed mid-way). Wipe it so
    # the fresh-clone path below can recreate it — `git clone` refuses to
    # write into a non-empty directory. Stuck task #156 root cause.
    if os.path.exists(workspace):
        shutil.rmtree(workspace)

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
        if user_id is not None:
            try:
                await install_coauthor_hook(workspace, user_id)
            except Exception as e:
                import structlog
                structlog.get_logger().warning(
                    "coauthor_hook_install_failed",
                    user_id=user_id, error=str(e),
                )
        await _run_git("checkout", "-b", default_branch, cwd=workspace, check=True)
        await _run_git("push", "-u", "origin", default_branch, cwd=workspace, check=True)
        return workspace

    await _run_git("clone", "-b", default_branch, authed_url, workspace, check=True)

    # Configure local git identity so the agent's commits work in containers
    # that have no global gitconfig. Local config overrides nothing upstream
    # but satisfies `git commit`'s identity requirement.
    await _run_git("config", "user.email", _AGENT_GIT_EMAIL, cwd=workspace)
    await _run_git("config", "user.name", _AGENT_GIT_NAME, cwd=workspace)

    # Per-user attribution — install a commit-msg hook so every commit
    # carries a Co-Authored-By trailer for the task's owner. Best-effort:
    # any failure here is logged but doesn't break the clone.
    if user_id is not None:
        try:
            await install_coauthor_hook(workspace, user_id)
        except Exception as e:
            import structlog
            structlog.get_logger().warning(
                "coauthor_hook_install_failed", user_id=user_id, error=str(e),
            )

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


# commit-msg hook that appends the Co-Authored-By trailer for the task's
# owner. Reads a trailer line from `.git/auto-agent-coauthor` (written at
# clone time by ``install_coauthor_hook``) and appends it to the commit
# message if it isn't already present. Idempotent — re-running git commit
# on the same message doesn't duplicate the trailer.
_COAUTHOR_HOOK_SCRIPT = """#!/bin/sh
# Auto-agent commit-msg hook — appends Co-Authored-By for the task owner.
trailer_file="$(git rev-parse --git-dir)/auto-agent-coauthor"
[ ! -f "$trailer_file" ] && exit 0
trailer="$(cat "$trailer_file")"
[ -z "$trailer" ] && exit 0
# Skip if the trailer is already present in the message.
grep -qF "$trailer" "$1" && exit 0
# Append, ensuring a blank line separates the trailer block from the body
# (git's trailer parser requires it).
last_line="$(tail -n1 "$1")"
if [ -n "$last_line" ]; then
    printf '\\n%s\\n' "$trailer" >> "$1"
else
    printf '%s\\n' "$trailer" >> "$1"
fi
"""


async def install_coauthor_hook(workspace: str, user_id: int) -> None:
    """Install a commit-msg hook that auto-appends Co-Authored-By for the
    requesting user. Looks up the user's display_name + username from the DB.

    Best-effort: callers wrap this in a try/except — a missing user or DB
    hiccup shouldn't block task execution.
    """
    from sqlalchemy import select

    from shared.database import async_session
    from shared.models import User

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()

    if user is None:
        return

    display = (user.display_name or user.username or "User").strip()
    trailer = f"Co-Authored-By: {display} <{user.username}@auto-agent.local>"

    git_dir = os.path.join(workspace, ".git")
    if not os.path.isdir(git_dir):
        return

    trailer_path = os.path.join(git_dir, "auto-agent-coauthor")
    with open(trailer_path, "w", encoding="utf-8") as f:
        f.write(trailer + "\n")

    hooks_dir = os.path.join(git_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    hook_path = os.path.join(hooks_dir, "commit-msg")
    with open(hook_path, "w", encoding="utf-8") as f:
        f.write(_COAUTHOR_HOOK_SCRIPT)
    os.chmod(hook_path, 0o755)


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
    import structlog
    _log = structlog.get_logger()

    # `git status --porcelain` lists both unstaged + untracked files
    status, _, _ = await _run_git("status", "--porcelain", cwd=workspace)
    if not status.strip():
        # Log workspace state for debugging empty-branch failures
        log_out, _, _ = await _run_git("log", "--oneline", "-5", cwd=workspace)
        _log.info(
            "commit_safety_net_nothing_to_commit",
            task_id=task_id,
            recent_commits=log_out.strip()[:200],
        )
        return False
    _log.info(
        "commit_safety_net_found_uncommitted",
        task_id=task_id,
        status=status.strip()[:500],
    )

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


def cleanup_workspace(task_id: int, organization_id: int | None = None) -> None:
    """Remove a task's workspace.

    Accepts organization_id so per-org sub-trees can be cleaned. None preserves
    the legacy `<WORKSPACES_DIR>/task-<task_id>` path for back-compat with
    callers that haven't been updated yet.
    """
    workspace = _workspace_path(task_id=task_id, organization_id=organization_id)
    if os.path.exists(workspace):
        shutil.rmtree(workspace)
