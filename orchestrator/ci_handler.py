"""CI-resolution handler — decides the next state when AWAITING_CI resolves.

The trio integration PR uses the same ``AWAITING_CI`` state as a regular
PR: when the CI run completes, the orchestrator needs to either advance
to review (on pass) or pick a retry path (on fail). For a regular task
the retry path is ``AWAITING_CI → CODING`` so the builder can take
another swing. For a trio parent (``complex_large`` with a populated
``trio_backlog`` and no parent), we instead re-enter
``TRIO_EXECUTING`` so the architect can plan a repair pass.

The detection is precise — all three of ``complexity == COMPLEX_LARGE``,
``parent_task_id is None``, and ``trio_backlog is not None`` are
required. A child of a trio parent (which has ``parent_task_id`` set)
should follow the regular non-trio retry path.

This function is the single source of truth for AWAITING_CI resolution.
Webhook and poller code paths should funnel through here rather than
hard-coding the transition.
"""
from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import select

from orchestrator.state_machine import transition
from shared.database import async_session
from shared.models import Task, TaskComplexity, TaskStatus

logger = structlog.get_logger()


async def on_ci_resolved(task_id: int, *, passed: bool, log: str) -> None:
    """Resolve a task that was sitting in ``AWAITING_CI``.

    Idempotent — if the task is not (or no longer) in ``AWAITING_CI``,
    returns without doing anything.

    On pass: ``AWAITING_CI → AWAITING_REVIEW``.
    On fail for a trio parent: ``AWAITING_CI → TRIO_EXECUTING`` and
    fires ``run_trio_parent`` with a ``repair_context`` carrying the CI
    log and the failed PR URL.
    On fail for any other task: ``AWAITING_CI → CODING`` (the existing
    non-trio retry path).
    """
    async with async_session() as s:
        task = (
            await s.execute(select(Task).where(Task.id == task_id))
        ).scalar_one_or_none()
        if task is None or task.status != TaskStatus.AWAITING_CI:
            return

        if passed:
            await transition(
                s, task, TaskStatus.AWAITING_REVIEW, "CI passed",
            )
            await s.commit()
            return

        is_trio_parent = (
            task.complexity == TaskComplexity.COMPLEX_LARGE
            and task.parent_task_id is None
            and task.trio_backlog is not None
        )

        if is_trio_parent:
            repair_context: dict[str, object] = {
                "ci_log": log,
                "failed_pr_url": task.pr_url,
            }
            await transition(
                s,
                task,
                TaskStatus.TRIO_EXECUTING,
                "CI failed on integration PR — re-entering trio for repair",
            )
            await s.commit()

            # Snapshot the fields ``run_trio_parent`` reads from the
            # passed-in ``parent`` before the session closes; the trio
            # orchestrator re-loads the row inside its own session, so
            # this object only needs to carry identity through the
            # asyncio.create_task hand-off.
            from agent.lifecycle.trio import run_trio_parent

            logger.info(
                "ci_handler.trio_parent_repair_re_entry",
                task_id=task_id,
                pr_url=task.pr_url,
            )
            # Fire-and-forget: the webhook/poller caller returns
            # immediately. The trio orchestrator re-loads the row in its
            # own session, so the unawaited task is safe to leak.
            asyncio.create_task(  # noqa: RUF006
                run_trio_parent(task, repair_context=repair_context),
            )
            return

        # Non-trio retry path — let the regular coding loop take another
        # swing with the failure context surfaced via the caller's event.
        await transition(
            s, task, TaskStatus.CODING, "CI failed — retrying",
        )
        await s.commit()
