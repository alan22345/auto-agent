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


async def find_duplicate_by_source_id(session: AsyncSession, source_id: str) -> Task | None:
    if not source_id:
        return None
    result = await session.execute(
        select(Task).where(
            Task.source_id == source_id,
            Task.status.in_(LIVE_STATUSES),
        )
    )
    return result.scalar_one_or_none()


async def find_duplicate_by_title(session: AsyncSession, title: str) -> Task | None:
    result = await session.execute(
        select(Task).where(
            Task.title == title,
            Task.status.in_(LIVE_STATUSES),
        )
    )
    return result.scalar_one_or_none()
