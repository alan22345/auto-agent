"""Regression test for the broken DELETE /tasks/{id}.

The child FKs (task_history / verify_attempts / review_attempts) all carry
``ON DELETE CASCADE`` at the DB level (migrations 032 + 044), but the ORM
relationships on ``Task`` lacked ``passive_deletes=True``. So SQLAlchemy
ignored the DB cascade and instead tried to NULL the children's ``task_id``
on parent delete — which ``task_history.task_id NOT NULL`` rejects, 500-ing
every task delete (``UPDATE task_history SET task_id=NULL`` IntegrityError).

This pins that deleting a task with history rows cascades cleanly.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from shared.models import (
    Organization,
    Plan,
    Task,
    TaskComplexity,
    TaskHistory,
    TaskSource,
    TaskStatus,
)


async def _seed_org(session) -> Organization:
    plan = Plan(
        name=f"plan-{uuid.uuid4().hex[:6]}",
        max_concurrent_tasks=2,
        max_tasks_per_day=100,
        max_input_tokens_per_day=10_000_000,
        max_output_tokens_per_day=2_500_000,
        max_members=5,
    )
    session.add(plan)
    await session.flush()
    org = Organization(
        name=f"org-{uuid.uuid4().hex[:6]}",
        slug=f"org-{uuid.uuid4().hex[:8]}",
        plan_id=plan.id,
    )
    session.add(org)
    await session.flush()
    return org


@pytest.mark.asyncio
async def test_delete_task_cascades_history_rows(session):
    """Deleting a task with task_history rows must succeed and remove the
    history (DB ON DELETE CASCADE), not raise a NOT NULL violation."""
    org = await _seed_org(session)
    task = Task(
        title="to delete",
        description="to delete",
        source=TaskSource.MANUAL,
        status=TaskStatus.QUEUED,
        complexity=TaskComplexity.SIMPLE,
        organization_id=org.id,
    )
    session.add(task)
    await session.flush()
    task_id = task.id

    session.add_all(
        [
            TaskHistory(
                task_id=task_id,
                from_status=None,
                to_status=TaskStatus.QUEUED,
                message="created",
            ),
            TaskHistory(
                task_id=task_id,
                from_status=TaskStatus.QUEUED,
                to_status=TaskStatus.CODING,
                message="started",
            ),
        ]
    )
    await session.flush()

    # The delete itself is what regressed — before the fix this raised
    # IntegrityError trying to NULL task_history.task_id.
    await session.delete(task)
    await session.flush()

    remaining = (
        await session.execute(
            select(func.count(TaskHistory.id)).where(TaskHistory.task_id == task_id)
        )
    ).scalar_one()
    assert remaining == 0
