"""Trio reviewer — alignment check between builder output and architect intent.

Runs between ``VERIFYING`` and ``PR_CREATED`` for trio children
(``parent_task_id is not None``). Dispatched by ``verify._pass_cycle``
as an ``asyncio.create_task`` so the verify call returns promptly.

Verdict path:
  * ``ok=true`` → child transitions to ``PR_CREATED`` and ``_open_pr_and_advance``
    runs (push branch + open PR).
  * ``ok=false`` or invalid JSON → child transitions back to ``CODING`` with
    the feedback as the retry reason, so the next coding cycle picks it up.

Cycle numbering is per-child (``trio_review_attempts.cycle`` counts up
across loop-backs for the same child).
"""
from __future__ import annotations

import json
import re
from typing import Any

import structlog
from sqlalchemy import select

from agent.lifecycle._naming import _fresh_session_id
from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.lifecycle.trio.prompts import TRIO_REVIEWER_SYSTEM
from shared.database import async_session
from shared.models import Task, TaskStatus, TrioReviewAttempt

log = structlog.get_logger()

_JSON_RE = re.compile(r"```json\s*(\{[\s\S]*?\})\s*```", re.MULTILINE)


def _extract_verdict(text: str) -> dict | None:
    """Extract ``{"ok": bool, "feedback": str}`` from a fenced JSON block.

    Handles:
      - No JSON block → ``None``
      - Malformed JSON → skip that block, try the next
      - Missing ``ok`` key → skip that block
      - Multiple blocks → prefer the last valid one

    Returns ``None`` on total failure. ``feedback`` defaults to ``""`` when
    absent so callers can rely on it being present.
    """
    if not text:
        return None
    matches = list(_JSON_RE.finditer(text))
    if not matches:
        return None
    for m in reversed(matches):
        try:
            v = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(v, dict) or "ok" not in v:
            continue
        v.setdefault("feedback", "")
        return v
    return None


def _result_output(result: Any) -> str:
    """Extract text output from whatever ``agent.run`` returned."""
    if hasattr(result, "output"):
        return result.output or ""
    return str(result) if result is not None else ""


def _result_tool_calls(result: Any, agent: Any) -> list:
    """Best-effort tool-call log — mirrors the architect helper."""
    for src in (agent, result):
        log_attr = getattr(src, "tool_call_log", None)
        if isinstance(log_attr, list):
            return log_attr
    calls = getattr(result, "tool_calls", None)
    if isinstance(calls, list):
        return calls
    return []


async def _prepare_review_workspace(
    *,
    child_id: int,
    repo_url: str,
    parent_branch: str,
    user_id: int | None,
    organization_id: int | None,
) -> str:
    """Clone (or reuse) the child's workspace at the integration branch.

    Reuses the path ``agent.workspace.clone_repo`` allocates for
    ``task_id=child_id`` — which is the same dir coding/verify just used,
    so the child's branch is already checked out with its commits on
    top of the integration branch. The diff is visible without any
    further checkout.

    Falls through to a fresh clone if the workspace is gone (e.g. after
    cleanup). The reviewer doesn't strictly need the child's local
    commits — the child branch was pushed by coding's ``push_branch``
    before verify ran — but reusing avoids a duplicate clone and keeps
    the diff visible against the integration branch.
    """
    from agent.workspace import clone_repo

    return await clone_repo(
        repo_url,
        child_id,
        parent_branch,
        user_id=user_id,
        organization_id=organization_id,
    )


def _create_reviewer_agent(
    workspace: str,
    task_id: int,
    task_description: str,
    repo_name: str | None,
    home_dir: str | None,
    org_id: int | None,
):
    """Build the ``AgentLoop`` for the trio reviewer."""
    session_id = _fresh_session_id(task_id, "trio-review")
    agent = create_agent(
        workspace=workspace,
        session_id=session_id,
        task_id=task_id,
        task_description=task_description,
        with_browser=True,  # optional spot-check via browse_url
        max_turns=30,
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    agent.system_prompt_override = TRIO_REVIEWER_SYSTEM
    return agent


async def handle_trio_review(
    child_task_id: int,
    *,
    workspace: str | None = None,
    parent_branch: str | None = None,
) -> None:
    """Run the reviewer for one builder cycle and act on its verdict.

    ``workspace`` and ``parent_branch`` are optional plumbing for callers
    that already have a prepared workspace (verify's ``_pass_cycle``);
    when omitted the reviewer clones afresh at the parent's integration
    branch.
    """
    async with async_session() as session:
        child = (
            await session.execute(select(Task).where(Task.id == child_task_id))
        ).scalar_one()
        if child.parent_task_id is None:
            log.warning(
                "trio.review.skipped_non_trio_child", child_id=child_task_id,
            )
            return
        # Snapshot everything we'll need outside the session (the reviewer
        # run is long; we don't want to hold a connection or trip lazy
        # loads on a detached instance later).
        parent_id = child.parent_task_id
        child_description = child.description or child.title
        repo_name = child.repo.name if child.repo else None
        repo_url = child.repo.url if child.repo else None
        user_id = child.created_by_user_id
        org_id = child.organization_id
        home_dir = await home_dir_for_task(child)
        # If verify didn't hand us a parent_branch, derive it.
        effective_parent_branch = parent_branch or f"trio/{parent_id}"

    if workspace is None:
        if repo_url is None:
            # Cold-start parents have no repo; reviewer has nothing to clone.
            # The caller should not reach this path in practice — trio
            # children inherit the parent's repo_id when one exists.
            log.error(
                "trio.review.no_workspace_no_repo", child_id=child_task_id,
            )
            return
        workspace = await _prepare_review_workspace(
            child_id=child_task_id,
            repo_url=repo_url,
            parent_branch=effective_parent_branch,
            user_id=user_id,
            organization_id=org_id,
        )

    prompt = (
        f"== Work item description (also PR body) ==\n{child_description}\n\n"
        "Review the diff in this workspace against ARCHITECTURE.md and the "
        "work item.\n"
        f"Run `git diff {effective_parent_branch}...HEAD` to see what changed.\n"
        "End your message with the verdict JSON block."
    )
    agent = _create_reviewer_agent(
        workspace=workspace,
        task_id=child_task_id,
        task_description=child_description,
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
    )
    run_result = await agent.run(prompt)
    output = _result_output(run_result)
    tool_calls = _result_tool_calls(run_result, agent)
    verdict = _extract_verdict(output)

    async with async_session() as session:
        child = (
            await session.execute(select(Task).where(Task.id == child_task_id))
        ).scalar_one()
        existing = (
            await session.execute(
                select(TrioReviewAttempt).where(
                    TrioReviewAttempt.task_id == child.id
                )
            )
        ).scalars().all()
        cycle = len(existing) + 1

        if verdict is None:
            feedback = (
                "Reviewer produced invalid JSON. Please re-state the changes "
                "made and re-trigger review."
            )
            session.add(TrioReviewAttempt(
                task_id=child.id,
                cycle=cycle,
                ok=False,
                feedback=feedback,
                tool_calls=tool_calls,
            ))
            from orchestrator.state_machine import transition
            await transition(
                session, child, TaskStatus.CODING,
                message="trio review: invalid reviewer JSON",
            )
            await session.commit()
            log.info(
                "trio.review.invalid_json",
                child_id=child_task_id, cycle=cycle,
            )
            return

        ok = bool(verdict.get("ok"))
        feedback = str(verdict.get("feedback", ""))
        session.add(TrioReviewAttempt(
            task_id=child.id,
            cycle=cycle,
            ok=ok,
            feedback=feedback,
            tool_calls=tool_calls,
        ))
        if not ok:
            from orchestrator.state_machine import transition
            await transition(
                session, child, TaskStatus.CODING,
                message=f"trio review failed (cycle {cycle}): {feedback[:200]}",
            )
        # On ok=true we DON'T transition here — _open_pr_and_advance handles
        # the push and PR creation, then independent review takes the task
        # through PR_CREATED → AWAITING_CI itself.
        await session.commit()

    log.info(
        "trio.review.complete",
        child_id=child_task_id, cycle=cycle, ok=ok,
    )

    if ok:
        # Hand off to the existing PR-opening path. The trio child's base
        # branch is the parent's integration branch (set by coding.py for
        # any task with parent_task_id) — so _open_pr_and_advance pushes
        # the feature branch and opens a PR back into trio/<parent_id>.
        from agent.lifecycle._orchestrator_api import get_task
        from agent.lifecycle.coding import _open_pr_and_advance

        task_data = await get_task(child_task_id)
        if task_data is None or not task_data.branch_name:
            log.error(
                "trio.review.cannot_open_pr_missing_branch",
                child_id=child_task_id,
            )
            return
        await _open_pr_and_advance(
            child_task_id,
            task_data,
            workspace,
            effective_parent_branch,
            task_data.branch_name,
        )
