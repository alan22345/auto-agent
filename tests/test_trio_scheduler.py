"""Tests for ``agent.lifecycle.trio.scheduler``.

Mirrors the pattern in ``test_architect_checkpoint`` /
``test_architect_run_initial``: the real DB session is used so we
exercise actual schema writes (Task row creation, ``Task.trio_backlog``
mutation) against the trio-migrated schema.

``scheduler.async_session`` is patched to a factory that yields the
test's transaction-wrapped session so every write lands inside the
per-test savepoint and rolls back cleanly.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import inspect, select

from agent.lifecycle.trio import scheduler
from agent.lifecycle.trio.scheduler import await_child, dispatch_next
from shared.models import (
    Organization,
    Plan,
    Task,
    TaskComplexity,
    TaskSource,
    TaskStatus,
    TrioPhase,
)


async def _skip_if_trio_columns_missing(session) -> None:
    """Skip the test if the connected DB hasn't run the trio migration."""
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


async def _seed_parent(
    session,
    *,
    backlog: list[dict] | None = None,
    freeform_mode: bool = False,
) -> Task:
    org = await _seed_org(session)
    t = Task(
        title="Build app",
        description="Build app",
        source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING,
        complexity=TaskComplexity.COMPLEX_LARGE,
        trio_phase=TrioPhase.AWAITING_BUILDER,
        organization_id=org.id,
        trio_backlog=backlog,
        freeform_mode=freeform_mode,
    )
    session.add(t)
    await session.flush()
    return t


async def _seed_existing_child(
    session, parent: Task, *, status: TaskStatus = TaskStatus.CODING,
) -> Task:
    t = Task(
        title="existing child",
        description="existing child",
        source=TaskSource.MANUAL,
        status=status,
        complexity=TaskComplexity.COMPLEX,
        organization_id=parent.organization_id,
        parent_task_id=parent.id,
    )
    session.add(t)
    await session.flush()
    return t


def _patched_async_session(real_session):
    """Build a factory that yields ``real_session`` from ``async with``."""
    real_session.commit = AsyncMock(side_effect=lambda: real_session.flush())
    real_session.close = AsyncMock()
    real_session.refresh = AsyncMock()

    @asynccontextmanager
    async def _factory():
        yield real_session

    return _factory


# ---------------------------------------------------------------------------
# dispatch_next
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_next_creates_child_and_marks_item_in_progress(
    session, monkeypatch,
):
    """Parent has 2 pending items, no assigned children. dispatch_next picks
    the first one. Returns a child Task: parent_task_id=parent.id,
    status=QUEUED, description=item.description, complexity=COMPLEX,
    freeform_mode inherited from parent. Parent's trio_backlog now has the
    first item with status='in_progress' and assigned_task_id=child.id."""
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(
        session,
        backlog=[
            {"id": "w1", "title": "auth", "description": "Add auth", "status": "pending"},
            {"id": "w2", "title": "ingredients", "description": "Add ingredients", "status": "pending"},
        ],
        freeform_mode=True,
    )
    parent_id = parent.id

    monkeypatch.setattr(scheduler, "async_session", _patched_async_session(session))

    child = await dispatch_next(parent)

    assert child is not None
    assert child.parent_task_id == parent_id
    assert child.status == TaskStatus.QUEUED
    assert child.complexity == TaskComplexity.COMPLEX
    assert child.description == "Add auth"
    assert child.freeform_mode is True
    assert child.organization_id == parent.organization_id

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.trio_backlog[0]["status"] == "in_progress"
    assert refreshed.trio_backlog[0]["assigned_task_id"] == child.id
    # Second item still pending — only first dispatched.
    assert refreshed.trio_backlog[1]["status"] == "pending"
    assert "assigned_task_id" not in refreshed.trio_backlog[1] or \
        refreshed.trio_backlog[1].get("assigned_task_id") is None


@pytest.mark.asyncio
async def test_dispatch_next_is_idempotent_on_already_assigned(
    session, monkeypatch,
):
    """Parent has an item already in_progress with assigned_task_id pointing
    to an existing Task. dispatch_next returns the EXISTING Task, doesn't
    create a duplicate."""
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session, backlog=[
        {"id": "w1", "title": "auth", "description": "Add auth", "status": "pending"},
    ])
    existing = await _seed_existing_child(session, parent)
    parent.trio_backlog = [
        {
            "id": "w1",
            "title": "auth",
            "description": "Add auth",
            "status": "in_progress",
            "assigned_task_id": existing.id,
        },
        {"id": "w2", "title": "ingredients", "description": "Add ingredients", "status": "pending"},
    ]
    await session.flush()

    parent_id = parent.id
    existing_id = existing.id

    monkeypatch.setattr(scheduler, "async_session", _patched_async_session(session))

    # Count tasks pre-call so we can verify nothing new was inserted.
    pre = (
        await session.execute(select(Task).where(Task.parent_task_id == parent_id))
    ).scalars().all()
    pre_ids = {t.id for t in pre}

    child = await dispatch_next(parent)

    assert child is not None
    assert child.id == existing_id

    post = (
        await session.execute(select(Task).where(Task.parent_task_id == parent_id))
    ).scalars().all()
    post_ids = {t.id for t in post}
    assert post_ids == pre_ids


@pytest.mark.asyncio
async def test_dispatch_next_returns_none_when_backlog_drained(
    session, monkeypatch,
):
    """All items are done. dispatch_next returns None."""
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session, backlog=[
        {"id": "w1", "title": "auth", "description": "...", "status": "done"},
        {"id": "w2", "title": "ingredients", "description": "...", "status": "done"},
    ])

    monkeypatch.setattr(scheduler, "async_session", _patched_async_session(session))

    result = await dispatch_next(parent)
    assert result is None


@pytest.mark.asyncio
async def test_dispatch_next_returns_none_when_backlog_empty(
    session, monkeypatch,
):
    """Empty / null backlog returns None gracefully."""
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session, backlog=None)

    monkeypatch.setattr(scheduler, "async_session", _patched_async_session(session))

    result = await dispatch_next(parent)
    assert result is None


# ---------------------------------------------------------------------------
# await_child
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_await_child_resolves_on_done(session, monkeypatch):
    """Child starts in CODING. After a small delay, flip child.status to DONE.
    await_child resolves with the refreshed Task whose status is DONE."""
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session, backlog=[])
    child = await _seed_existing_child(session, parent, status=TaskStatus.CODING)
    child_id = child.id

    monkeypatch.setattr(scheduler, "async_session", _patched_async_session(session))
    # Shorten the poll interval so the test runs quickly.
    monkeypatch.setattr(scheduler, "_POLL_INTERVAL_S", 0.05)

    async def _flip_to_done():
        await asyncio.sleep(0.1)
        child.status = TaskStatus.DONE
        await session.flush()

    flip_task = asyncio.create_task(_flip_to_done())
    try:
        refreshed = await asyncio.wait_for(await_child(parent, child), timeout=3.0)
    finally:
        await flip_task

    assert refreshed.id == child_id
    assert refreshed.status == TaskStatus.DONE


@pytest.mark.asyncio
async def test_await_child_returns_immediately_if_already_terminal(
    session, monkeypatch,
):
    """If child is already DONE/FAILED/BLOCKED at call time, return at once."""
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session, backlog=[])
    child = await _seed_existing_child(session, parent, status=TaskStatus.FAILED)

    monkeypatch.setattr(scheduler, "async_session", _patched_async_session(session))
    monkeypatch.setattr(scheduler, "_POLL_INTERVAL_S", 0.05)

    refreshed = await asyncio.wait_for(await_child(parent, child), timeout=1.0)
    assert refreshed.status == TaskStatus.FAILED
