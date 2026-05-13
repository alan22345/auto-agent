"""Trio recovery — resume in-flight trio parents on orchestrator startup."""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import select

from shared.database import async_session
from shared.events import Event, TaskEventType, publish
from shared.models import ArchitectAttempt, Task, TaskStatus

log = structlog.get_logger()


async def resume_all_trio_parents() -> None:
    """Find every task in TRIO_EXECUTING and dispatch run_trio_parent for it.

    Called once at orchestrator startup. The orchestrator + architect modules
    enforce idempotency internally, so re-running them on tasks that already
    have committed work is safe.
    """
    from agent.lifecycle.trio import run_trio_parent

    async with async_session() as s:
        rows = (
            (await s.execute(select(Task).where(Task.status == TaskStatus.TRIO_EXECUTING)))
            .scalars()
            .all()
        )

    if rows:
        log.info(
            "trio.recovery.resuming",
            count=len(rows),
            task_ids=[r.id for r in rows],
        )
        for parent in rows:
            asyncio.create_task(run_trio_parent(parent))  # noqa: RUF006

    # AWAITING_CLARIFICATION + trio_phase set. The architect is
    # paused; if the answer landed pre-crash we re-publish RESOLVED so
    # on_architect_clarification_resolved transitions state and calls
    # architect.resume. If no answer yet, do nothing — we're still
    # waiting on a human / PO.
    async with async_session() as s:
        awaiting = (
            (
                await s.execute(
                    select(Task)
                    .where(Task.status == TaskStatus.AWAITING_CLARIFICATION)
                    .where(Task.trio_phase.is_not(None))
                )
            )
            .scalars()
            .all()
        )
        for task in awaiting:
            latest = (
                await s.execute(
                    select(ArchitectAttempt)
                    .where(ArchitectAttempt.task_id == task.id)
                    .where(ArchitectAttempt.clarification_question.is_not(None))
                    .order_by(ArchitectAttempt.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if latest is None:
                continue
            if latest.clarification_answer is None:
                log.info(
                    "trio.recovery.awaiting_clarification.still_waiting",
                    task_id=task.id,
                )
                continue
            log.info(
                "trio.recovery.awaiting_clarification.republish_resolved",
                task_id=task.id,
            )
            await publish(
                Event(
                    type=TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED,
                    task_id=task.id,
                )
            )
