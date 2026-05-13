"""Tests for ``orchestrator.ci_handler.on_ci_resolved``.

The trio integration PR shares ``AWAITING_CI`` with regular tasks, so
the handler needs to distinguish three outcomes:

* CI passed → ``AWAITING_REVIEW`` (regardless of trio-ness).
* CI failed on a trio parent → re-enter ``TRIO_EXECUTING`` with a
  ``repair_context`` so the architect can plan a repair pass.
* CI failed on any other task → fall back to ``CODING`` (the existing
  non-trio retry path).

Same shape as the other trio tests in this suite: real DB session,
``async_session`` patched to a factory that yields the test's
transaction-wrapped session so every write rolls back cleanly. The
asyncio-fire-and-forget hand-off into ``run_trio_parent`` is patched
so the test asserts on the call args rather than the trio cycle's
side-effects.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import inspect, select

from shared.models import (
    Organization,
    Plan,
    Task,
    TaskComplexity,
    TaskSource,
    TaskStatus,
)


async def _skip_if_trio_columns_missing(session) -> None:
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


async def _seed_task(
    session,
    *,
    status: TaskStatus,
    complexity: TaskComplexity,
    parent_task_id: int | None = None,
    trio_backlog: list | None = None,
    pr_url: str | None = None,
) -> Task:
    org = await _seed_org(session)
    t = Task(
        title="Build app",
        description="Build app",
        source=TaskSource.MANUAL,
        status=status,
        complexity=complexity,
        organization_id=org.id,
        parent_task_id=parent_task_id,
        trio_backlog=trio_backlog,
        pr_url=pr_url,
    )
    session.add(t)
    await session.flush()
    return t


def _patched_async_session(real_session):
    """Yield ``real_session`` from ``async with async_session() as s``.

    ``commit`` is forwarded to a flush so writes are visible to the test
    but still inside the per-test savepoint that rolls back at teardown.
    """
    real_session.commit = AsyncMock(side_effect=lambda: real_session.flush())
    real_session.close = AsyncMock()

    @asynccontextmanager
    async def _factory():
        yield real_session

    return _factory


# ---------------------------------------------------------------------------
# Trio parent: AWAITING_CI failure → re-enters TRIO_EXECUTING with repair ctx.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parent_awaiting_ci_failure_re_enters_trio_executing(session):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_task(
        session,
        status=TaskStatus.AWAITING_CI,
        complexity=TaskComplexity.COMPLEX_LARGE,
        trio_backlog=[
            {"id": "w1", "title": "x", "description": "x", "status": "done"},
        ],
        pr_url="https://github.com/x/y/pull/3",
    )
    parent_id = parent.id

    from orchestrator import ci_handler

    with (
        patch.object(ci_handler, "async_session", _patched_async_session(session)),
        patch(
            "agent.lifecycle.trio.run_trio_parent", new=AsyncMock(),
        ) as m,
    ):
        await ci_handler.on_ci_resolved(
            parent_id, passed=False, log="TypeError: foo",
        )
        # Give the asyncio.create_task a moment to schedule.
        await asyncio.sleep(0.05)

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.TRIO_EXECUTING

    m.assert_awaited_once()
    repair_ctx = m.await_args.kwargs.get("repair_context")
    assert repair_ctx is not None
    assert "TypeError: foo" in repair_ctx["ci_log"]
    assert repair_ctx["failed_pr_url"] == "https://github.com/x/y/pull/3"


# ---------------------------------------------------------------------------
# Pass path: AWAITING_CI → AWAITING_REVIEW.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parent_awaiting_ci_pass_proceeds_to_awaiting_review(session):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_task(
        session,
        status=TaskStatus.AWAITING_CI,
        complexity=TaskComplexity.COMPLEX_LARGE,
        trio_backlog=[
            {"id": "w1", "title": "x", "description": "x", "status": "done"},
        ],
        pr_url="https://github.com/x/y/pull/3",
    )
    parent_id = parent.id

    from orchestrator import ci_handler

    with patch.object(
        ci_handler, "async_session", _patched_async_session(session),
    ):
        await ci_handler.on_ci_resolved(parent_id, passed=True, log="")

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.AWAITING_REVIEW


# ---------------------------------------------------------------------------
# Non-trio task (no parent, no trio_backlog) failing CI → CODING.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_trio_task_ci_failure_falls_back_to_existing_behaviour(session):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_task(
        session,
        status=TaskStatus.AWAITING_CI,
        complexity=TaskComplexity.COMPLEX,
        trio_backlog=None,
        pr_url="https://github.com/x/y/pull/3",
    )
    parent_id = parent.id

    from orchestrator import ci_handler

    with patch.object(
        ci_handler, "async_session", _patched_async_session(session),
    ):
        await ci_handler.on_ci_resolved(
            parent_id, passed=False, log="some error",
        )

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.CODING


# ---------------------------------------------------------------------------
# Idempotency: task not in AWAITING_CI → no-op.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_when_not_in_awaiting_ci(session):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_task(
        session,
        status=TaskStatus.DONE,
        complexity=TaskComplexity.SIMPLE,
    )
    parent_id = parent.id

    from orchestrator import ci_handler

    with patch.object(
        ci_handler, "async_session", _patched_async_session(session),
    ):
        await ci_handler.on_ci_resolved(parent_id, passed=True, log="")

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.DONE


# ---------------------------------------------------------------------------
# Trio child (has parent_task_id) failing CI → CODING (not TRIO_EXECUTING).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trio_child_ci_failure_is_not_treated_as_trio_parent(session):
    """Even with COMPLEX_LARGE, a task with ``parent_task_id`` is a child
    and should follow the regular non-trio retry path."""
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_task(
        session,
        status=TaskStatus.TRIO_EXECUTING,
        complexity=TaskComplexity.COMPLEX_LARGE,
        trio_backlog=[
            {"id": "w1", "title": "x", "description": "x", "status": "pending"},
        ],
    )
    child = await _seed_task(
        session,
        status=TaskStatus.AWAITING_CI,
        complexity=TaskComplexity.COMPLEX,
        parent_task_id=parent.id,
        pr_url="https://github.com/x/y/pull/4",
    )
    child_id = child.id

    from orchestrator import ci_handler

    with (
        patch.object(ci_handler, "async_session", _patched_async_session(session)),
        patch(
            "agent.lifecycle.trio.run_trio_parent", new=AsyncMock(),
        ) as m,
    ):
        await ci_handler.on_ci_resolved(child_id, passed=False, log="oops")
        await asyncio.sleep(0.05)

    refreshed = (
        await session.execute(select(Task).where(Task.id == child_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.CODING
    m.assert_not_awaited()
