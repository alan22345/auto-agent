"""Integration PR CI failure triggers architect-driven repair, not BLOCKED.

Regression for the architect_checkpoint repair flow added in Task 17
(orchestrator.ci_handler.on_ci_resolved).

This test focuses specifically on the **repair-context handshake**: when a
trio-parent's integration PR fails CI, ``on_ci_resolved`` must:

1. Transition the parent from ``AWAITING_CI`` → ``TRIO_EXECUTING``.
2. Call ``run_trio_parent`` with a populated ``repair_context`` dict that
   carries ``ci_log`` and ``failed_pr_url`` so the architect can plan a
   targeted fix pass.

The dispatch decision itself is covered by ``test_trio_ci_failure_re_entry.py``
(Task 17). This file is the dedicated regression guard for the *content* of
the repair handoff.
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


# ---------------------------------------------------------------------------
# Helpers (same shape as test_trio_ci_failure_re_entry.py)
# ---------------------------------------------------------------------------


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
        trio_backlog=trio_backlog,
        pr_url=pr_url,
    )
    session.add(t)
    await session.flush()
    return t


def _patched_async_session(real_session):
    """Yield ``real_session`` from ``async with async_session() as s``.

    ``commit`` is forwarded to a flush so writes are visible within the test
    but stay inside the per-test savepoint that rolls back at teardown.
    """
    real_session.commit = AsyncMock(side_effect=lambda: real_session.flush())
    real_session.close = AsyncMock()

    @asynccontextmanager
    async def _factory():
        yield real_session

    return _factory


# ---------------------------------------------------------------------------
# Regression: repair-context handshake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ci_failure_re_enters_trio_with_repair_context(session):
    """When the parent's integration PR fails CI, ``on_ci_resolved``:

    1. Transitions parent ``AWAITING_CI`` → ``TRIO_EXECUTING``.
    2. Schedules ``run_trio_parent`` with a ``repair_context`` dict that
       contains ``ci_log`` (the raw failure log) and ``failed_pr_url``
       (the PR that broke CI) so the architect can produce a targeted fix.
    """
    await _skip_if_trio_columns_missing(session)

    parent = await _seed_task(
        session,
        status=TaskStatus.AWAITING_CI,
        complexity=TaskComplexity.COMPLEX_LARGE,
        trio_backlog=[
            {"id": "w1", "title": "auth", "description": "x", "status": "done"},
        ],
        pr_url="https://github.com/x/y/pull/3",
    )
    parent_id = parent.id
    ci_log = "TypeError: cannot import name 'foo' from 'bar'"

    from orchestrator import ci_handler

    with (
        patch.object(ci_handler, "async_session", _patched_async_session(session)),
        patch(
            "agent.lifecycle.trio.run_trio_parent",
            new=AsyncMock(),
        ) as mock_run,
    ):
        await ci_handler.on_ci_resolved(parent_id, passed=False, log=ci_log)
        # Allow the asyncio.create_task to be scheduled and awaited.
        await asyncio.sleep(0.05)

    # --- Status transition ---
    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.TRIO_EXECUTING, (
        f"Expected TRIO_EXECUTING after CI failure; got {refreshed.status}"
    )

    # --- Repair-context handshake ---
    mock_run.assert_awaited_once()
    repair_ctx = mock_run.await_args.kwargs.get("repair_context")
    assert repair_ctx is not None, (
        "run_trio_parent must receive a 'repair_context' kwarg so the "
        "architect can plan a targeted fix pass"
    )
    assert "TypeError" in repair_ctx["ci_log"], (
        f"repair_context['ci_log'] should contain the failure log; got: {repair_ctx['ci_log']!r}"
    )
    assert repair_ctx["failed_pr_url"] == "https://github.com/x/y/pull/3", (
        f"repair_context['failed_pr_url'] mismatch: {repair_ctx['failed_pr_url']!r}"
    )
