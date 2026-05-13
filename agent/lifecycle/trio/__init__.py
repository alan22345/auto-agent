"""Architect/builder/reviewer trio lifecycle orchestration.

``run_trio_parent`` is the entry point: it drives a parent task through
its trio phases (architecting → awaiting_builder → architect_checkpoint),
loops until the backlog is drained, and opens the final integration PR
back to the target branch.

Phase transitions are persisted on ``Task.trio_phase`` so an external
observer (UI, debugging) can see where in the cycle we are. The outer
status transitions (``TRIO_EXECUTING → PR_CREATED`` / ``→ BLOCKED``) go
through ``orchestrator.state_machine.transition`` to enforce the
allowed-transitions check and log a ``TaskHistory`` row.
"""
from __future__ import annotations

import structlog
from sqlalchemy import select

from agent.lifecycle.trio import architect, scheduler
from orchestrator.state_machine import transition
from shared.database import async_session
from shared.models import Task, TaskStatus, TrioPhase

log = structlog.get_logger()


async def _set_trio_phase(parent_id: int, phase: TrioPhase | None) -> None:
    """Load the parent, set ``trio_phase``, commit. Avoids stale refs."""
    async with async_session() as s:
        p = (
            await s.execute(select(Task).where(Task.id == parent_id))
        ).scalar_one()
        p.trio_phase = phase
        await s.commit()


async def _resolve_target_branch(parent_id: int) -> str:
    """Pick the integration PR target branch for the parent.

    Non-freeform parents always target ``main``. Freeform parents target
    the repo's ``FreeformConfig.dev_branch`` when one exists, falling
    back to ``main`` otherwise.
    """
    from shared.models import FreeformConfig

    async with async_session() as s:
        p = (
            await s.execute(select(Task).where(Task.id == parent_id))
        ).scalar_one()
        if not p.freeform_mode or p.repo_id is None:
            return "main"
        cfg = (
            await s.execute(
                select(FreeformConfig).where(FreeformConfig.repo_id == p.repo_id)
            )
        ).scalar_one_or_none()
        if cfg is None:
            return "main"
        return cfg.dev_branch or "main"


async def _open_integration_pr(parent: Task, target_branch: str) -> str:
    """Open the final ``trio/<parent_id> → target_branch`` PR via gh CLI.

    The integration branch already lives on the remote (every child PR
    merge pushed to it), so we do not push from here. We just shell out
    to ``gh pr create``, using the same auth pattern Task 12 used for
    the init PR (``shared.github_auth.get_github_token`` →
    ``GH_TOKEN`` env). Returns the PR URL (gh prints it on stdout).

    Best-effort: if gh fails or no auth is available, returns an empty
    string and logs the failure rather than crashing the orchestrator.
    """
    from agent import sh
    from shared.github_auth import get_github_token

    integration_branch = f"trio/{parent.id}"

    try:
        token = await get_github_token(
            user_id=parent.created_by_user_id,
            organization_id=parent.organization_id,
        )
    except Exception as e:  # pragma: no cover — env-dependent
        log.warning(
            "trio.parent.gh_token_unavailable",
            parent_id=parent.id, error=str(e),
        )
        return ""

    gh_env = {"GH_TOKEN": token} if token else {}

    title = f"trio: integration — {parent.title}"
    body = (
        f"Final integration PR for trio parent #{parent.id}.\n\n"
        "Contains every child PR that landed on the integration branch "
        f"`{integration_branch}` during the trio cycle."
    )

    create_res = await sh.run(
        [
            "gh", "pr", "create",
            "--base", target_branch,
            "--head", integration_branch,
            "--title", title,
            "--body", body,
        ],
        timeout=30,
        env=gh_env,
    )
    if create_res.failed:
        log.warning(
            "trio.parent.integration_pr_create_failed",
            parent_id=parent.id,
            stderr=(create_res.stderr or "")[:500],
        )
        return ""
    return (create_res.stdout or "").strip()


async def run_trio_parent(
    parent: Task,
    *,
    repair_context: dict | None = None,
) -> None:
    """Drive a parent task through the trio cycle, open the final PR.

    Fresh entry (``repair_context is None``) runs the architect's
    initial pass. Re-entry from a failed integration PR threads the CI
    log into a checkpoint pass so the architect can add fix work items.

    Each iteration: re-read the parent's backlog, dispatch the next
    pending item, await the child, run a checkpoint, and act on the
    architect's decision (``continue`` / ``revise`` / ``done`` /
    ``blocked``). On any failed/blocked child or ``blocked`` decision
    the parent transitions to ``BLOCKED`` and we return early. When the
    backlog is drained we open the final integration PR and transition
    to ``PR_CREATED``.
    """
    if repair_context is None:
        await _set_trio_phase(parent.id, TrioPhase.ARCHITECTING)
        await architect.run_initial(parent.id)
    else:
        await _set_trio_phase(parent.id, TrioPhase.ARCHITECT_CHECKPOINT)
        await architect.checkpoint(parent.id, repair_context=repair_context)

    while True:
        # Re-read the backlog each iteration in case a revision added or
        # removed items.
        async with async_session() as s:
            p = (
                await s.execute(select(Task).where(Task.id == parent.id))
            ).scalar_one()
            if p.status == TaskStatus.BLOCKED:
                # ``architect.run_initial`` (or a prior iteration) already
                # blocked us — nothing left to do.
                return
            if p.status == TaskStatus.AWAITING_CLARIFICATION:
                # Architect emitted awaiting_clarification — the parent is
                # paused waiting for PO (freeform) or user answers. The
                # dispatcher in run.py picks up
                # ARCHITECT_CLARIFICATION_RESOLVED and calls
                # ``architect.resume`` which re-enters the trio orchestrator.
                # Exit cleanly here so we don't dispatch a child while
                # the architect is still designing.
                log.info(
                    "trio.parent.paused_for_clarification",
                    parent_id=parent.id,
                )
                return
            backlog = p.trio_backlog or []
            pending = [it for it in backlog if it.get("status") == "pending"]
            if not pending:
                break

        await _set_trio_phase(parent.id, TrioPhase.AWAITING_BUILDER)
        child = await scheduler.dispatch_next(parent)
        if child is None:
            break

        finished = await scheduler.await_child(parent, child)
        if finished.status in (TaskStatus.FAILED, TaskStatus.BLOCKED):
            async with async_session() as s:
                p = (
                    await s.execute(select(Task).where(Task.id == parent.id))
                ).scalar_one()
                await transition(
                    s, p, TaskStatus.BLOCKED,
                    message=(
                        f"trio: child #{finished.id} terminated "
                        f"{finished.status.value}"
                    ),
                )
                p.trio_phase = None
                await s.commit()
            log.info(
                "trio.parent.blocked_on_child",
                parent_id=parent.id,
                child_id=finished.id,
                child_status=finished.status.value,
            )
            return

        await _set_trio_phase(parent.id, TrioPhase.ARCHITECT_CHECKPOINT)
        decision = await architect.checkpoint(
            parent.id, child_task_id=finished.id,
        )
        action = decision.get("action", "blocked")

        if action == "revise":
            await _set_trio_phase(parent.id, TrioPhase.ARCHITECTING)
            await architect.run_revision(parent.id)
            continue
        if action == "awaiting_clarification":
            # checkpoint emitted clarification — _emit_clarification has
            # already transitioned the parent to AWAITING_CLARIFICATION.
            # Exit cleanly; resume runs after the answer lands.
            log.info(
                "trio.parent.paused_for_clarification_at_checkpoint",
                parent_id=parent.id,
            )
            return
        if action == "done":
            break
        if action == "blocked":
            async with async_session() as s:
                p = (
                    await s.execute(select(Task).where(Task.id == parent.id))
                ).scalar_one()
                await transition(
                    s, p, TaskStatus.BLOCKED,
                    message=(
                        f"trio: architect.checkpoint returned blocked — "
                        f"{decision.get('reason', '')}"
                    ),
                )
                p.trio_phase = None
                await s.commit()
            log.info(
                "trio.parent.blocked_on_checkpoint",
                parent_id=parent.id,
                reason=decision.get("reason"),
            )
            return
        # action == "continue" → loop iterates and dispatches next item.

    # Backlog drained — open the final integration PR.
    target = await _resolve_target_branch(parent.id)

    async with async_session() as s:
        p = (
            await s.execute(select(Task).where(Task.id == parent.id))
        ).scalar_one()
        pr_url = await _open_integration_pr(p, target)
        p.pr_url = pr_url or None
        p.trio_phase = None
        await transition(s, p, TaskStatus.PR_CREATED, message="trio: integration PR opened")
        await s.commit()

    log.info(
        "trio.parent.opened_final_pr",
        parent_id=parent.id,
        pr_url=pr_url,
        target_branch=target,
    )
