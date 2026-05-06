"""Task queue — single global pool with per-repo cap.

Concurrency rules:
  - At most ``settings.max_concurrent_workers`` tasks active at once.
  - At most 1 active task per repo_id (prevents working-tree conflicts).
  - Tasks with repo_id IS NULL (e.g. SIMPLE_NO_CODE research) bypass the
    per-repo cap; only the global cap applies.
  - BLOCKED_ON_AUTH is paused, not active — does not occupy a slot.

FIFO across all users. Priority (lower = first) breaks ties; default 100.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from shared.config import settings
from shared.models import Task, TaskStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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


async def count_active(session: AsyncSession) -> int:
    """Total active tasks across all users and repos."""
    result = await session.execute(
        select(func.count(Task.id)).where(Task.status.in_(ACTIVE_STATUSES))
    )
    return result.scalar_one()


async def _repo_has_active_task(session: AsyncSession, repo_id: int) -> bool:
    result = await session.execute(
        select(func.count(Task.id)).where(
            Task.repo_id == repo_id,
            Task.status.in_(ACTIVE_STATUSES),
        )
    )
    return result.scalar_one() > 0


async def can_start_task(session: AsyncSession, task: Task) -> bool:
    """Can this specific task start right now?"""
    if await count_active(session) >= settings.max_concurrent_workers:
        return False
    return not (
        task.repo_id is not None
        and await _repo_has_active_task(session, task.repo_id)
    )


async def next_eligible_task(session: AsyncSession) -> Task | None:
    """Return the highest-priority QUEUED task that can start right now.

    Iterates queued tasks in (priority asc, created_at asc) order and returns
    the first one that passes can_start_task. A repo-blocked task is skipped
    so other repos' tasks aren't head-of-line-blocked.
    """
    if await count_active(session) >= settings.max_concurrent_workers:
        return None

    # Snapshot all currently-active repo_ids in one query.
    active_repos_q = await session.execute(
        select(Task.repo_id)
        .where(Task.status.in_(ACTIVE_STATUSES), Task.repo_id.is_not(None))
        .distinct()
    )
    busy_repos = {row[0] for row in active_repos_q.all()}

    queued_q = await session.execute(
        select(Task)
        .where(Task.status == TaskStatus.QUEUED)
        .order_by(Task.priority.asc(), Task.created_at.asc())
    )
    for t in queued_q.scalars():
        if t.repo_id is None or t.repo_id not in busy_repos:
            return t
    return None
