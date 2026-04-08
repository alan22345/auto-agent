"""Task queue — enforces max concurrent tasks (1 complex + 1 simple)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import settings
from shared.models import Task, TaskComplexity, TaskStatus

# Statuses that count as "active" (occupying a slot)
ACTIVE_STATUSES = {
    TaskStatus.PLANNING,
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.AWAITING_CLARIFICATION,
    TaskStatus.CODING,
    TaskStatus.PR_CREATED,
    TaskStatus.AWAITING_CI,
    TaskStatus.AWAITING_REVIEW,
    TaskStatus.BLOCKED,
}


async def count_active(session: AsyncSession, complexity: TaskComplexity) -> int:
    """Count how many tasks of this complexity are currently active."""
    result = await session.execute(
        select(func.count(Task.id)).where(
            Task.complexity == complexity,
            Task.status.in_(ACTIVE_STATUSES),
        )
    )
    return result.scalar_one()


async def can_start(session: AsyncSession, complexity: TaskComplexity) -> bool:
    """Check if there's a slot available for this complexity."""
    active = await count_active(session, complexity)
    if complexity == TaskComplexity.COMPLEX:
        return active < settings.max_concurrent_complex
    return active < settings.max_concurrent_simple


async def next_queued_task(session: AsyncSession, complexity: TaskComplexity) -> Task | None:
    """Get the oldest queued task of the given complexity."""
    result = await session.execute(
        select(Task)
        .where(Task.complexity == complexity, Task.status == TaskStatus.QUEUED)
        .order_by(Task.created_at.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()
