"""Trio recovery — resume in-flight trio parents on orchestrator startup."""
from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import select

from shared.database import async_session
from shared.models import Task, TaskStatus

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
            await s.execute(
                select(Task).where(Task.status == TaskStatus.TRIO_EXECUTING)
            )
        ).scalars().all()

    if not rows:
        return

    log.info(
        "trio.recovery.resuming",
        count=len(rows),
        task_ids=[r.id for r in rows],
    )
    for parent in rows:
        asyncio.create_task(run_trio_parent(parent))  # noqa: RUF006
