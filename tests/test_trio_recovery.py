"""Tests for trio crash recovery (Task 23).

Verifies that ``resume_all_trio_parents`` finds tasks stuck in TRIO_EXECUTING
and dispatches ``run_trio_parent`` for each of them — and that tasks in other
statuses are left alone.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import inspect

from shared.models import (
    Organization,
    Plan,
    Task,
    TaskComplexity,
    TaskSource,
    TaskStatus,
)

# ---------------------------------------------------------------------------
# Helpers shared with test_trio_routing.py
# ---------------------------------------------------------------------------

async def _skip_if_trio_columns_missing(session) -> None:
    """Skip cleanly when the DB hasn't run the trio migration."""
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")

    def _trio_cols(sync_conn) -> set[str]:
        insp = inspect(sync_conn)
        cols = {c["name"] for c in insp.get_columns("tasks")}
        return cols & {"parent_task_id", "trio_phase", "trio_backlog"}

    conn = await session.connection()
    present = await conn.run_sync(_trio_cols)
    if len(present) < 3:
        pytest.skip(
            "trio columns not present in DATABASE_URL "
            "— run `alembic upgrade head`",
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


async def _seed_task_with_status(session, *, status: TaskStatus) -> Task:
    """Seed a task directly in the given status."""
    org = await _seed_org(session)
    t = Task(
        title="Stuck trio parent",
        description="Some task stuck in flight",
        source=TaskSource.MANUAL,
        status=status,
        complexity=TaskComplexity.COMPLEX_LARGE,
        organization_id=org.id,
        freeform_mode=False,
        created_by_user_id=None,
    )
    session.add(t)
    await session.flush()
    return t


def _patched_async_session(real_session):
    """Yield ``real_session`` from ``async with async_session() as s``.

    Forwards ``commit`` to ``flush`` so writes are visible inside the test
    transaction but still roll back at teardown.
    """
    real_session.commit = AsyncMock(side_effect=lambda: real_session.flush())
    real_session.close = AsyncMock()
    real_session.refresh = AsyncMock()

    @asynccontextmanager
    async def _factory():
        yield real_session

    return _factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recovery_resumes_parent_in_trio_executing(session, monkeypatch):
    """A task in TRIO_EXECUTING gets re-dispatched via run_trio_parent.

    ``repair_context`` must be None (fresh re-entry, not a CI-repair path).
    """
    await _skip_if_trio_columns_missing(session)

    task = await _seed_task_with_status(session, status=TaskStatus.TRIO_EXECUTING)

    import agent.lifecycle.trio.recovery as recovery_mod
    monkeypatch.setattr(
        recovery_mod,
        "async_session",
        _patched_async_session(session),
    )

    run_trio_parent_mock = AsyncMock()
    with patch("agent.lifecycle.trio.run_trio_parent", new=run_trio_parent_mock):
        from agent.lifecycle.trio.recovery import resume_all_trio_parents
        await resume_all_trio_parents()
        # Give asyncio.create_task a turn to run the coroutine.
        await asyncio.sleep(0.05)

    run_trio_parent_mock.assert_awaited_once()
    # First positional arg must be the Task row.
    assert run_trio_parent_mock.await_args.args[0].id == task.id
    # Must NOT pass repair_context.
    assert run_trio_parent_mock.await_args.kwargs.get("repair_context") is None


@pytest.mark.asyncio
async def test_recovery_ignores_non_trio_tasks(session, monkeypatch):
    """A task in CODING is not picked up by trio recovery."""
    await _skip_if_trio_columns_missing(session)

    await _seed_task_with_status(session, status=TaskStatus.CODING)

    import agent.lifecycle.trio.recovery as recovery_mod
    monkeypatch.setattr(
        recovery_mod,
        "async_session",
        _patched_async_session(session),
    )

    run_trio_parent_mock = AsyncMock()
    with patch("agent.lifecycle.trio.run_trio_parent", new=run_trio_parent_mock):
        from agent.lifecycle.trio.recovery import resume_all_trio_parents
        await resume_all_trio_parents()
        await asyncio.sleep(0)

    run_trio_parent_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_handles_multiple_stuck_parents(session, monkeypatch):
    """Multiple TRIO_EXECUTING parents are all dispatched."""
    await _skip_if_trio_columns_missing(session)

    t1 = await _seed_task_with_status(session, status=TaskStatus.TRIO_EXECUTING)
    t2 = await _seed_task_with_status(session, status=TaskStatus.TRIO_EXECUTING)

    import agent.lifecycle.trio.recovery as recovery_mod
    monkeypatch.setattr(
        recovery_mod,
        "async_session",
        _patched_async_session(session),
    )

    run_trio_parent_mock = AsyncMock()
    with patch("agent.lifecycle.trio.run_trio_parent", new=run_trio_parent_mock):
        from agent.lifecycle.trio.recovery import resume_all_trio_parents
        await resume_all_trio_parents()
        await asyncio.sleep(0.05)

    assert run_trio_parent_mock.await_count == 2
    dispatched_ids = {
        call.args[0].id for call in run_trio_parent_mock.await_args_list
    }
    assert dispatched_ids == {t1.id, t2.id}
