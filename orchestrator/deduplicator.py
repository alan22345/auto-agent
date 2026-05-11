"""Task deduplication — detect duplicate tasks from multiple sources.

Uses two strategies:
1. Exact source_id match (same Linear ticket, same Slack ts)
2. Exact title match
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Task, TaskStatus

LIVE_STATUSES = {
    TaskStatus.INTAKE,
    TaskStatus.CLASSIFYING,
    TaskStatus.QUEUED,
    TaskStatus.PLANNING,
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.CODING,
    TaskStatus.PR_CREATED,
    TaskStatus.AWAITING_CI,
    TaskStatus.AWAITING_REVIEW,
    TaskStatus.BLOCKED,
}


async def find_duplicate_by_source_id(
    session: AsyncSession,
    source_id: str,
    *,
    organization_id: int | None = None,
) -> Task | None:
    """Find a live task with the same source_id, optionally scoped to an org.

    Cross-org dedup must NOT collapse: two tenants receiving the same Slack
    message ID (rare but possible) should each get their own task.
    """
    if not source_id:
        return None
    q = select(Task).where(
        Task.source_id == source_id,
        Task.status.in_(LIVE_STATUSES),
    )
    if organization_id is not None:
        q = q.where(Task.organization_id == organization_id)
    result = await session.execute(q)
    return result.scalar_one_or_none()


async def find_duplicate_by_title(
    session: AsyncSession,
    title: str,
    *,
    organization_id: int | None = None,
) -> Task | None:
    q = select(Task).where(
        Task.title == title,
        Task.status.in_(LIVE_STATUSES),
    )
    if organization_id is not None:
        q = q.where(Task.organization_id == organization_id)
    result = await session.execute(q)
    return result.scalar_one_or_none()
