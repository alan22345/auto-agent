"""Sweep that promotes BLOCKED_ON_QUOTA → QUEUED when usage falls under cap.

Runs on the same periodic interval as the queue dispatcher in run.py.
At UTC midnight, sum_tokens_today resets to 0, so this sweep effectively
unblocks every quota-paused task as soon as the clock ticks over.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from orchestrator.state_machine import transition
from shared import quotas
from shared.models import Task, TaskStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


async def unblock_quota_paused(session: AsyncSession) -> int:
    """Return the number of tasks moved back to QUEUED."""
    q = await session.execute(
        select(Task).where(Task.status == TaskStatus.BLOCKED_ON_QUOTA)
    )
    moved = 0
    for t in q.scalars():
        if t.organization_id is None:
            continue
        try:
            plan = await quotas.get_plan_for_org(session, t.organization_id)
        except LookupError:
            continue
        in_used, out_used = await quotas.sum_tokens_today(session, t.organization_id)
        if (
            in_used < plan.max_input_tokens_per_day
            and out_used < plan.max_output_tokens_per_day
        ):
            await transition(session, t, TaskStatus.QUEUED, "Quota window reset")
            moved += 1
    if moved:
        log.info("unblocked_quota_paused_tasks", count=moved)
    return moved
