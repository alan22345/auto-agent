"""Merge-conflict resolution — runs when a freeform PR can't auto-merge.

Triggered by `task.merge_conflict_detected`. Clones the feature branch,
merges the base branch into it (no rebase, no force-push), and if conflicts
arise, hands off to the agent to resolve them. On a clean result, commits
the merge and pushes. Emits `task.merge_conflict_resolved` on success or
`task.merge_conflict_resolution_failed` on giveup.
"""

from __future__ import annotations

import httpx

from agent.workspace import _run_git, clone_repo
from shared.config import settings
from shared.events import (
    publish,
    task_merge_conflict_resolution_failed,
    task_merge_conflict_resolved,
)
from shared.logging import setup_logging
from shared.types import RepoData, TaskData

log = setup_logging("conflict-resolver")

ORCHESTRATOR_URL = settings.orchestrator_url


CONFLICT_RESOLUTION_PROMPT = """\
You are resolving a git merge conflict on an auto-agent feature branch.

The base branch ({base_branch}) was just merged into this feature branch and
some files conflict. Your job is to resolve every conflict marker so the merge
can be committed.

## What to do

1. Run `git status` to list all files with conflicts.
2. For each file: read it, understand both sides, and produce a correct merged
   version. Prefer the base branch's structural changes (renames, refactors)
   while preserving the feature branch's new behavior. Keep the resulting code
   correct and consistent — do not just pick one side blindly when both
   contributed real changes.
3. After editing each file, `git add <file>` it.
4. When all files are resolved, verify `git status` shows no remaining
   conflict markers (no `<<<<<<<`, `=======`, `>>>>>>>`).
5. Do NOT run `git commit`. The orchestrator will commit the merge once you
   finish — your job ends after staging the resolved files.
6. Do NOT push. Do NOT change git config. Do NOT touch any branch other than
   the current one.

## Rules

- You may read and edit any file in the repo if needed to keep the merge
  consistent (e.g. updating an import after a rename in the base branch).
- Run tests or linters if you want, but it is not required — the CI on the
  resulting PR will catch regressions.
- If a conflict is genuinely unresolvable (e.g. the same line was changed in
  contradictory ways with no obvious correct answer), output a single line
  starting with `RESOLUTION_FAILED:` followed by a one-sentence explanation
  and stop.
"""


async def _get_task(task_id: int) -> TaskData | None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/tasks/{task_id}")
        if resp.status_code == 200:
            return TaskData.model_validate(resp.json())
    return None


async def _get_repo(repo_name: str) -> RepoData | None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}/repos")
        if resp.status_code != 200:
            return None
        for raw in resp.json():
            r = RepoData.model_validate(raw)
            if r.name == repo_name:
                return r
    return None


def _parse_pr_url(pr_url: str) -> tuple[str, str, str]:
    parts = pr_url.rstrip("/").split("/")
    return parts[-4], parts[-3], parts[-1]


async def _fetch_pr_branches(pr_url: str) -> tuple[str, str] | None:
    """Returns (head_branch, base_branch) from GitHub or None on failure."""
    if not settings.github_token:
        return None
    owner, repo, num = _parse_pr_url(pr_url)
    headers = {
        "Authorization": f"token {settings.github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{num}",
                headers=headers,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            head = data.get("head", {}).get("ref")
            base = data.get("base", {}).get("ref")
            if not head or not base:
                return None
            return head, base
    except Exception:
        log.exception(f"Failed to fetch PR branches for {pr_url}")
        return None


async def _has_conflict_markers(workspace: str) -> bool:
    """Check whether any tracked file still contains git conflict markers."""
    out, _, _ = await _run_git("diff", "--check", cwd=workspace)
    return "leftover conflict marker" in out.lower() or "conflict marker" in out.lower()


async def _emit_failed(task_id: int, reason: str) -> None:
    log.warning(f"Conflict resolution failed for task #{task_id}: {reason}")
    await publish(task_merge_conflict_resolution_failed(task_id, reason=reason))


async def handle_merge_conflict_resolution(task_id: int, pr_url: str) -> None:
    """Resolve a merge conflict on a freeform PR. Emits success/failure event."""
    log.info(f"Conflict resolver starting for task #{task_id} ({pr_url})")

    task = await _get_task(task_id)
    if not task:
        await _emit_failed(task_id, "task not found")
        return
    if not task.repo_name:
        await _emit_failed(task_id, "task has no repo")
        return

    repo = await _get_repo(task.repo_name)
    if not repo:
        await _emit_failed(task_id, f"repo '{task.repo_name}' not found")
        return

    branches = await _fetch_pr_branches(pr_url)
    if not branches:
        await _emit_failed(task_id, "could not fetch PR branches from GitHub")
        return
    head_branch, base_branch = branches

    # Clone at the feature branch directly. The clone helper auto-injects the
    # GH token so subsequent fetch/push work without further auth setup.
    try:
        workspace = await clone_repo(
            repo.url,
            task_id=task_id,
            default_branch=head_branch,
            workspace_name=f"conflict-resolve-{task_id}",
        )
    except Exception:
        log.exception(f"Clone failed for conflict resolution task #{task_id}")
        await _emit_failed(task_id, f"could not clone {head_branch}")
        return

    # Make sure we have a fresh view of the base branch before merging.
    _, fetch_err, fetch_rc = await _run_git("fetch", "origin", base_branch, cwd=workspace)
    if fetch_rc != 0:
        await _emit_failed(task_id, f"git fetch origin {base_branch} failed: {fetch_err.strip()[:200]}")
        return

    # Attempt the merge. A 0 return code means no conflicts — straight commit.
    # A non-zero with "CONFLICT" in stderr means we need the agent.
    merge_out, merge_err, merge_rc = await _run_git(
        "merge", f"origin/{base_branch}",
        "--no-ff", "--no-edit",
        "-m", f"Merge branch '{base_branch}' into {head_branch} (auto-agent conflict resolution)",
        cwd=workspace,
    )
    combined = (merge_out + "\n" + merge_err).lower()

    if merge_rc == 0:
        log.info(f"Task #{task_id}: merge of {base_branch} clean, no agent needed")
        await _push_and_emit_success(workspace, head_branch, task_id)
        return

    if "conflict" not in combined:
        # Some other failure (e.g. uncommitted changes, missing ref). Abort.
        await _run_git("merge", "--abort", cwd=workspace)
        await _emit_failed(task_id, f"git merge failed: {merge_err.strip()[:200]}")
        return

    log.info(f"Task #{task_id}: merge has conflicts, dispatching agent")
    success = await _run_agent_resolution(workspace, base_branch, task_id)
    if not success:
        await _run_git("merge", "--abort", cwd=workspace)
        await _emit_failed(task_id, "agent could not resolve conflicts")
        return

    if await _has_conflict_markers(workspace):
        await _run_git("merge", "--abort", cwd=workspace)
        await _emit_failed(task_id, "conflict markers remain after agent resolution")
        return

    # Commit the merge. The agent staged the resolved files; we finalize.
    _, commit_err, commit_rc = await _run_git(
        "-c", "user.email=auto-agent@bot.local",
        "-c", "user.name=auto-agent",
        "commit", "--no-edit",
        cwd=workspace,
    )
    if commit_rc != 0:
        await _emit_failed(task_id, f"merge commit failed: {commit_err.strip()[:200]}")
        return

    await _push_and_emit_success(workspace, head_branch, task_id)


async def _push_and_emit_success(workspace: str, head_branch: str, task_id: int) -> None:
    # Regular push (no force) — we're adding a merge commit on top of the
    # existing feature branch, so fast-forward succeeds.
    _, push_err, push_rc = await _run_git("push", "origin", head_branch, cwd=workspace)
    if push_rc != 0:
        await _emit_failed(task_id, f"git push failed: {push_err.strip()[:200]}")
        return
    log.info(f"Task #{task_id}: conflict resolved and pushed to {head_branch}")
    await publish(task_merge_conflict_resolved(task_id, head_branch=head_branch))


async def _run_agent_resolution(workspace: str, base_branch: str, task_id: int) -> bool:
    """Run the agent loop in the workspace to resolve outstanding conflicts."""
    from agent.lifecycle.factory import create_agent

    agent = create_agent(workspace, readonly=False, max_turns=20, task_id=task_id)
    prompt = CONFLICT_RESOLUTION_PROMPT.format(base_branch=base_branch)
    try:
        result = await agent.run(prompt)
    except Exception:
        log.exception(f"Agent run errored during conflict resolution for task #{task_id}")
        return False

    output = (result.output or "").strip()
    if "RESOLUTION_FAILED:" in output:
        log.warning(f"Task #{task_id}: agent reported RESOLUTION_FAILED — {output[:300]}")
        return False
    return True
