"""Task state machine — enforces valid transitions and logs history."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Task, TaskComplexity, TaskHistory, TaskStatus

# Valid state transitions
TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.INTAKE: {TaskStatus.CLASSIFYING},
    TaskStatus.CLASSIFYING: {TaskStatus.QUEUED, TaskStatus.FAILED},
    TaskStatus.QUEUED: {TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.DONE},
    TaskStatus.PLANNING: {TaskStatus.AWAITING_APPROVAL, TaskStatus.AWAITING_CLARIFICATION, TaskStatus.FAILED, TaskStatus.BLOCKED},
    TaskStatus.AWAITING_APPROVAL: {TaskStatus.CODING, TaskStatus.PLANNING},  # approved or revision
    TaskStatus.AWAITING_CLARIFICATION: {TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.FAILED},  # user replied
    TaskStatus.CODING: {TaskStatus.PR_CREATED, TaskStatus.AWAITING_CLARIFICATION, TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.DONE},
    TaskStatus.PR_CREATED: {TaskStatus.AWAITING_CI},
    TaskStatus.AWAITING_CI: {TaskStatus.AWAITING_REVIEW, TaskStatus.CODING, TaskStatus.FAILED},  # CI pass/fail
    TaskStatus.AWAITING_REVIEW: {TaskStatus.DONE, TaskStatus.CODING},  # approved or request changes
    TaskStatus.BLOCKED: {TaskStatus.CODING, TaskStatus.PLANNING, TaskStatus.FAILED, TaskStatus.DONE},
    TaskStatus.DONE: set(),
    TaskStatus.FAILED: {TaskStatus.DONE},
}


class InvalidTransition(Exception):
    pass


async def transition(
    session: AsyncSession,
    task: Task,
    to_status: TaskStatus,
    message: str = "",
) -> Task:
    """Move a task to a new status, validating the transition and logging history."""
    from_status = task.status
    allowed = TRANSITIONS.get(from_status, set())

    if to_status not in allowed:
        raise InvalidTransition(
            f"Cannot transition from {from_status.value} to {to_status.value}. "
            f"Allowed: {[s.value for s in allowed]}"
        )

    task.status = to_status
    session.add(TaskHistory(
        task_id=task.id,
        from_status=from_status,
        to_status=to_status,
        message=message,
    ))
    await session.flush()
    return task


async def get_task(session: AsyncSession, task_id: int) -> Task | None:
    result = await session.execute(select(Task).where(Task.id == task_id))
    return result.scalar_one_or_none()
