"""Tests for Task-21 trio routing.

A queued task whose ``complexity == complex_large`` OR whose
``freeform_mode == True`` must be routed to ``TRIO_EXECUTING`` and
have ``run_trio_parent`` dispatched, instead of going down the
existing ``PLANNING`` / ``CODING`` paths.

Mirrors the shape of the other trio tests in this suite: real DB
session, ``async_session`` patched to a factory that yields the
test's transaction-wrapped session so writes roll back at teardown,
auth/concurrency seams stubbed to keep the test focused on the
routing decision.
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import inspect, select

import run as run_module
from shared.events import Event
from shared.models import (
    Organization,
    Plan,
    Task,
    TaskComplexity,
    TaskSource,
    TaskStatus,
)


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


async def _seed_queued_task(
    session,
    *,
    complexity: TaskComplexity,
    freeform_mode: bool = False,
) -> Task:
    """Seed a task already in CLASSIFYING — ``on_task_classified`` transitions
    it through QUEUED to its next state, so it must start one step back so
    the state machine accepts the moves."""
    org = await _seed_org(session)
    t = Task(
        title="Build app",
        description="Build app",
        source=TaskSource.MANUAL,
        status=TaskStatus.CLASSIFYING,
        complexity=complexity,
        organization_id=org.id,
        freeform_mode=freeform_mode,
        created_by_user_id=None,  # skip the dispatch-time auth probe
    )
    session.add(t)
    await session.flush()
    return t


def _patched_async_session(real_session):
    """Yield ``real_session`` from ``async with async_session() as s``.

    Forwards ``commit`` to ``flush`` so writes are visible to the test
    but still inside the per-test savepoint that rolls back at teardown.
    """
    real_session.commit = AsyncMock(side_effect=lambda: real_session.flush())
    real_session.close = AsyncMock()
    real_session.refresh = AsyncMock()

    @asynccontextmanager
    async def _factory():
        yield real_session

    return _factory


@pytest.mark.asyncio
async def test_complex_large_routes_to_trio(session, monkeypatch):
    """A queued complex_large task transitions to TRIO_EXECUTING and
    dispatches ``run_trio_parent`` (fire-and-forget)."""
    await _skip_if_trio_columns_missing(session)

    task = await _seed_queued_task(session, complexity=TaskComplexity.COMPLEX_LARGE)
    task_id = task.id

    monkeypatch.setattr(run_module, "async_session", _patched_async_session(session))
    # Always let it start — bypass the slot check.
    monkeypatch.setattr(run_module, "can_start_task", AsyncMock(return_value=True))

    run_trio_parent_mock = AsyncMock()
    with patch("agent.lifecycle.trio.run_trio_parent", new=run_trio_parent_mock):
        await run_module.on_task_classified(Event(type="classified", task_id=task_id))
        # Yield once so any asyncio.create_task scheduled fire-and-forget
        # awaits gets a chance to run.
        import asyncio
        await asyncio.sleep(0)

    refreshed = (
        await session.execute(select(Task).where(Task.id == task_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.TRIO_EXECUTING
    run_trio_parent_mock.assert_awaited_once()
    # Argument is the parent Task row.
    assert run_trio_parent_mock.await_args.args[0].id == task_id


@pytest.mark.asyncio
async def test_freeform_simple_routes_to_trio(session, monkeypatch):
    """A queued freeform task with simple complexity (NOT complex_large)
    still routes to TRIO_EXECUTING — the trio branch fires on EITHER
    condition."""
    await _skip_if_trio_columns_missing(session)

    task = await _seed_queued_task(
        session,
        complexity=TaskComplexity.SIMPLE,
        freeform_mode=True,
    )
    task_id = task.id

    monkeypatch.setattr(run_module, "async_session", _patched_async_session(session))
    monkeypatch.setattr(run_module, "can_start_task", AsyncMock(return_value=True))

    run_trio_parent_mock = AsyncMock()
    with patch("agent.lifecycle.trio.run_trio_parent", new=run_trio_parent_mock):
        await run_module.on_task_classified(Event(type="classified", task_id=task_id))
        import asyncio
        await asyncio.sleep(0)

    refreshed = (
        await session.execute(select(Task).where(Task.id == task_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.TRIO_EXECUTING
    run_trio_parent_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_freeform_simple_routes_to_existing_flow(session, monkeypatch):
    """A non-freeform simple task continues to CODING per existing behaviour.
    Trio dispatch must NOT fire."""
    await _skip_if_trio_columns_missing(session)

    task = await _seed_queued_task(
        session,
        complexity=TaskComplexity.SIMPLE,
        freeform_mode=False,
    )
    task_id = task.id

    monkeypatch.setattr(run_module, "async_session", _patched_async_session(session))
    monkeypatch.setattr(run_module, "can_start_task", AsyncMock(return_value=True))

    run_trio_parent_mock = AsyncMock()
    with patch("agent.lifecycle.trio.run_trio_parent", new=run_trio_parent_mock):
        await run_module.on_task_classified(Event(type="classified", task_id=task_id))
        import asyncio
        await asyncio.sleep(0)

    refreshed = (
        await session.execute(select(Task).where(Task.id == task_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.CODING
    run_trio_parent_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_freeform_complex_routes_to_existing_flow(session, monkeypatch):
    """A non-freeform complex (NOT complex_large) task continues to PLANNING
    per existing behaviour. Trio dispatch must NOT fire."""
    await _skip_if_trio_columns_missing(session)

    task = await _seed_queued_task(
        session,
        complexity=TaskComplexity.COMPLEX,
        freeform_mode=False,
    )
    task_id = task.id

    monkeypatch.setattr(run_module, "async_session", _patched_async_session(session))
    monkeypatch.setattr(run_module, "can_start_task", AsyncMock(return_value=True))

    run_trio_parent_mock = AsyncMock()
    with patch("agent.lifecycle.trio.run_trio_parent", new=run_trio_parent_mock):
        await run_module.on_task_classified(Event(type="classified", task_id=task_id))
        import asyncio
        await asyncio.sleep(0)

    refreshed = (
        await session.execute(select(Task).where(Task.id == task_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.PLANNING
    run_trio_parent_mock.assert_not_awaited()
