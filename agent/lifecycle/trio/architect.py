"""Architect agent for the trio lifecycle.

Four phases: initial, consult, checkpoint, revision. Each persists an
``ArchitectAttempt`` row scoped to the trio parent task.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Literal

import structlog
from sqlalchemy import select

from agent.lifecycle.factory import create_agent
from agent.lifecycle.trio.prompts import (
    ARCHITECT_CHECKPOINT_SYSTEM,
    ARCHITECT_CONSULT_SYSTEM,
    ARCHITECT_INITIAL_SYSTEM,
)
from shared.database import async_session
from shared.models import ArchitectAttempt, ArchitectPhase, Task, TaskStatus

log = structlog.get_logger()


_SYSTEM_PROMPTS = {
    "initial":    ARCHITECT_INITIAL_SYSTEM,
    "consult":    ARCHITECT_CONSULT_SYSTEM,
    "checkpoint": ARCHITECT_CHECKPOINT_SYSTEM,
    "revision":   ARCHITECT_INITIAL_SYSTEM,  # same shape as initial
}


def create_architect_agent(
    workspace: str,
    task_id: int,
    task_description: str,
    phase: Literal["initial", "consult", "checkpoint", "revision"],
    repo_name: str | None = None,
    home_dir: str | None = None,
    org_id: int | None = None,
):
    """Build an AgentLoop configured for the architect.

    The architect always has:
    - web_search + fetch_url (outside grounding)
    - record_decision + request_market_brief (its bespoke tools)
    - Standard file/bash/git tools
    """
    agent = create_agent(
        workspace=workspace,
        task_id=task_id,
        task_description=task_description,
        with_web=True,
        max_turns=80,
        include_methodology=False,
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    # Replace the default registry with one that adds the architect-only tools.
    from agent.tools import create_default_registry

    agent.tools = create_default_registry(
        with_web=True,
        with_architect_tools=True,
    )
    # Inject the phase-specific system prompt.
    agent.system_prompt_override = _SYSTEM_PROMPTS[phase]
    return agent


# ---------------------------------------------------------------------------
# run_initial — the architect's first pass over a trio parent task.
# ---------------------------------------------------------------------------

# Matches the trailing ```json { ... } ``` block the architect prompt asks for.
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{[\s\S]*?\})\s*```", re.MULTILINE)


def _extract_backlog(text: str) -> list[dict] | None:
    """Pull the ``{"backlog": [...]}`` JSON block out of the architect output.

    Returns the backlog list on success, ``None`` on any failure path:
    missing block, malformed JSON, no ``backlog`` key, item missing
    required fields. Items default to ``status="pending"`` when absent.
    """
    if not text:
        return None
    # Prefer the last JSON block — the architect's final pass may emit
    # multiple (e.g. an example illustration followed by the real one).
    matches = list(_JSON_BLOCK_RE.finditer(text))
    if not matches:
        return None
    for m in reversed(matches):
        try:
            payload = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        backlog = payload.get("backlog") if isinstance(payload, dict) else None
        if not isinstance(backlog, list) or not backlog:
            continue
        ok = True
        for item in backlog:
            if not isinstance(item, dict):
                ok = False
                break
            if not all(k in item for k in ("id", "title", "description")):
                ok = False
                break
            item.setdefault("status", "pending")
        if ok:
            return backlog
    return None


async def _prepare_parent_workspace(parent: Task) -> str:
    """Prepare a workspace for the architect's initial pass.

    With a repo attached: clone via ``agent.workspace.clone_repo`` and
    check out branch ``trio/<parent_id>`` off the repo's default branch
    (creating it if necessary).

    Without a repo (cold-start tasks like "build a TODO app"): allocate
    an empty directory under the workspaces root and ``git init`` it so
    the architect can scaffold + commit. The integration branch is still
    created under ``trio/<parent_id>`` for symmetry with the repo path.
    """
    from agent import sh
    from agent.workspace import (
        _AGENT_GIT_EMAIL,
        _AGENT_GIT_NAME,
        WORKSPACES_DIR,
        clone_repo,
        create_branch,
    )

    branch = f"trio/{parent.id}"

    if parent.repo is not None:
        base_branch = parent.repo.default_branch or "main"
        workspace = await clone_repo(
            parent.repo.url,
            parent.id,
            base_branch,
            user_id=parent.created_by_user_id,
            organization_id=parent.organization_id,
        )
        await create_branch(workspace, branch)
        return workspace

    # Repo-less cold start — bootstrap an empty git workspace.
    workspace = os.path.join(
        WORKSPACES_DIR, str(parent.organization_id), f"task-{parent.id}",
    )
    os.makedirs(workspace, exist_ok=True)
    if not os.path.isdir(os.path.join(workspace, ".git")):
        await sh.run(["git", "init", "-q"], cwd=workspace, timeout=30)
        await sh.run(
            ["git", "config", "user.email", _AGENT_GIT_EMAIL],
            cwd=workspace, timeout=10,
        )
        await sh.run(
            ["git", "config", "user.name", _AGENT_GIT_NAME],
            cwd=workspace, timeout=10,
        )
        # Seed an empty commit so HEAD exists before we branch off it.
        await sh.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=workspace, timeout=10,
        )
    await create_branch(workspace, branch)
    return workspace


async def _commit_and_open_initial_pr(parent: Task, workspace: str) -> str:
    """Commit scaffold + ARCHITECTURE.md, open the init PR, auto-merge.

    Sub-branches off the parent's integration branch ``trio/<parent_id>``
    to a fresh ``trio/<parent_id>/init`` head, commits anything the
    architect left in the working tree, pushes both branches, opens a
    PR back to the integration branch via the gh CLI, and asks GitHub to
    auto-squash-merge it.

    Returns the merged commit SHA on the integration branch (best-effort;
    falls back to HEAD of the head branch if no remote is reachable).
    """
    from agent import sh
    from agent.workspace import (
        commit_pending_changes,
        push_branch,
    )

    integration_branch = f"trio/{parent.id}"
    head_branch = f"trio/{parent.id}/init"

    # Sub-branch off the integration branch we're already on.
    await sh.run(
        ["git", "checkout", "-B", head_branch],
        cwd=workspace, timeout=30,
    )

    # Commit any pending scaffold + ARCHITECTURE.md the architect produced.
    # ``commit_pending_changes`` is a no-op when the working tree is clean.
    await commit_pending_changes(
        workspace, parent.id, f"init: architecture + scaffold — {parent.title}",
    )

    # Try to push and open a PR. If there's no remote configured (cold-start
    # workspaces without a repo), fall back to HEAD locally so callers still
    # get a usable SHA without crashing.
    remote_check = await sh.run(
        ["git", "remote"], cwd=workspace, timeout=10,
    )
    has_remote = bool((remote_check.stdout or "").strip())

    if has_remote:
        try:
            await push_branch(workspace, head_branch)
            # Make sure the integration branch exists upstream too so the PR
            # has somewhere to land.
            await sh.run(
                ["git", "push", "-u", "origin", integration_branch],
                cwd=workspace, timeout=30,
            )

            from shared.github_auth import get_github_token

            gh_env = {
                "GH_TOKEN": await get_github_token(
                    user_id=parent.created_by_user_id,
                    organization_id=parent.organization_id,
                ),
            }
            create_res = await sh.run(
                [
                    "gh", "pr", "create",
                    "--base", integration_branch,
                    "--head", head_branch,
                    "--title", f"init: architecture + scaffold — {parent.title}",
                    "--body", (
                        "Initial architecture pass for trio parent "
                        f"#{parent.id}.\n\n"
                        "Contains ARCHITECTURE.md, any scaffold the architect "
                        "produced, and seeds the integration branch for the "
                        "builder cycle."
                    ),
                ],
                cwd=workspace, timeout=30, env=gh_env,
            )
            if create_res.failed:
                log.warning(
                    "architect.initial_pr_create_failed",
                    task_id=parent.id,
                    stderr=(create_res.stderr or "")[:500],
                )
            else:
                # Auto-squash-merge — GitHub completes it once required checks
                # pass (or immediately when there are none).
                await sh.run(
                    ["gh", "pr", "merge", "--auto", "--squash"],
                    cwd=workspace, timeout=30, env=gh_env,
                )
        except Exception as e:  # pragma: no cover — best-effort remote plumbing
            log.warning(
                "architect.initial_pr_push_or_merge_failed",
                task_id=parent.id, error=str(e),
            )

    # Resolve the SHA we'll record. Prefer the local HEAD of the head branch
    # — that's what we committed, regardless of whether the auto-merge
    # completes synchronously.
    rev = await sh.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, timeout=10,
    )
    sha = (rev.stdout or "").strip()
    return sha[:40] if sha else ""


def _result_output(result: Any) -> str:
    """Extract the text output from whatever ``agent.run`` returned.

    Production path: ``AgentResult`` with an ``.output`` attribute.
    Test mocks: a string, or a ``MagicMock`` configured with ``output=``.
    """
    if hasattr(result, "output"):
        return result.output or ""
    return str(result) if result is not None else ""


def _result_tool_calls(result: Any, agent: Any) -> list:
    """Best-effort tool-call log for the ``ArchitectAttempt`` row.

    Looks first at the agent itself (mocks set ``tool_call_log``), then
    at the run result (``AgentResult.api_messages`` and friends), then
    falls back to an empty list.
    """
    for src in (agent, result):
        log_attr = getattr(src, "tool_call_log", None)
        if isinstance(log_attr, list):
            return log_attr
    calls = getattr(result, "tool_calls", None)
    if isinstance(calls, list):
        return calls
    return []


async def run_initial(parent_task_id: int) -> None:
    """Run the architect's initial pass on a trio parent task.

    On success: writes the integration commit SHA + backlog to the parent,
    persists an ``ArchitectAttempt`` row (``phase=INITIAL``, ``cycle=1``).
    On JSON-extraction failure: marks the parent ``BLOCKED`` and persists
    a blocked attempt row instead.
    """
    from agent.lifecycle.factory import home_dir_for_task

    async with async_session() as s:
        parent = (
            await s.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        # Pre-resolve everything we'll need outside the session — the
        # architect run is long, and we don't want a stale connection.
        task_description = parent.description or parent.title
        task_title = parent.title
        repo_name = parent.repo.name if parent.repo else None
        org_id = parent.organization_id
        home_dir = await home_dir_for_task(parent)

    workspace = await _prepare_parent_workspace(parent)

    agent = create_architect_agent(
        workspace=workspace,
        task_id=parent_task_id,
        task_description=task_description,
        phase="initial",
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    run_result = await agent.run(
        f"Run the initial architecture pass for: {task_title}\n\n"
        f"Task description:\n{task_description}",
    )
    output = _result_output(run_result)
    tool_calls = _result_tool_calls(run_result, agent)

    backlog = _extract_backlog(output)

    if backlog is None:
        log.error(
            "architect.run_initial.invalid_json",
            task_id=parent_task_id,
            output_preview=output[:300],
        )
        async with async_session() as s:
            parent = (
                await s.execute(select(Task).where(Task.id == parent_task_id))
            ).scalar_one()
            # Best-effort transition — if the parent isn't in a status that
            # allows BLOCKED, fall back to writing the attempt row only.
            try:
                from orchestrator.state_machine import transition

                await transition(
                    s, parent, TaskStatus.BLOCKED,
                    message="architect.run_initial: invalid JSON backlog",
                )
            except Exception:
                parent.status = TaskStatus.BLOCKED
            s.add(ArchitectAttempt(
                task_id=parent.id,
                phase=ArchitectPhase.INITIAL,
                cycle=1,
                reasoning=output,
                decision={
                    "action": "blocked",
                    "reason": "invalid JSON from architect",
                },
                tool_calls=tool_calls,
            ))
            await s.commit()
        return

    commit_sha = await _commit_and_open_initial_pr(parent, workspace)

    async with async_session() as s:
        parent = (
            await s.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        parent.trio_backlog = backlog
        s.add(ArchitectAttempt(
            task_id=parent.id,
            phase=ArchitectPhase.INITIAL,
            cycle=1,
            reasoning=output,
            commit_sha=commit_sha or None,
            tool_calls=tool_calls,
        ))
        await s.commit()

    log.info(
        "architect.run_initial.complete",
        task_id=parent_task_id,
        backlog_size=len(backlog),
        commit_sha=commit_sha,
    )


async def consult(*, parent_task_id: int, child_task_id: int, question: str, why: str):
    raise NotImplementedError("Task 13")


async def checkpoint(
    parent_task_id: int,
    *,
    child_task_id: int | None = None,
    repair_context: dict | None = None,
):
    raise NotImplementedError("Task 14")


async def run_revision(parent_task_id: int):
    raise NotImplementedError("Task 14")
