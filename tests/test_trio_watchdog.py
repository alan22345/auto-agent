"""Continuous trio watchdog — re-invoke the idempotent driver for stalled parents.

Trio previously had only boot-time recovery (``resume_all_trio_parents``), so a
parent that stalled mid-run — or a freeform task whose design-gate standin never
fired — sat forever, holding its repo, until a human deleted it. This watchdog
re-invokes ``run_trio_parent`` for parents stale past the threshold. A merely
slow parent (recent ``updated_at``) is left alone, so live work is never
disturbed; the driver is idempotent so re-entry is safe.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from shared.models import Organization, Plan, Task, TaskComplexity, TaskSource, TaskStatus

pytestmark = pytest.mark.asyncio


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
    org = Organization(name="o", slug=f"o-{uuid.uuid4().hex[:8]}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    return org


async def _seed_task(
    session, *, status: TaskStatus, age_minutes: int, freeform: bool = False
) -> Task:
    org = await _seed_org(session)
    t = Task(
        title="trio parent",
        description="",
        source=TaskSource.MANUAL,
        status=status,
        complexity=TaskComplexity.COMPLEX_LARGE,
        organization_id=org.id,
        freeform_mode=freeform,
        updated_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
    )
    session.add(t)
    await session.flush()
    return t


def _patched_async_session(real_session):
    real_session.commit = AsyncMock(side_effect=lambda: real_session.flush())
    real_session.close = AsyncMock()

    @asynccontextmanager
    async def _factory():
        yield real_session

    return _factory


async def _run_tick(session, monkeypatch) -> AsyncMock:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    import agent.lifecycle.trio.recovery as recovery_mod

    monkeypatch.setattr(recovery_mod, "async_session", _patched_async_session(session))
    mock = AsyncMock()
    with (
        patch("agent.lifecycle.trio.run_trio_parent", new=mock),
        patch("shared.notifier.send_telegram_async", new=AsyncMock()),
    ):
        await recovery_mod.resume_stalled_trio_parents_once()
        await asyncio.sleep(0.05)  # let create_task drain
    return mock


async def test_stalled_trio_parent_is_resumed(session, monkeypatch) -> None:
    task = await _seed_task(session, status=TaskStatus.TRIO_EXECUTING, age_minutes=120)
    mock = await _run_tick(session, monkeypatch)
    mock.assert_awaited_once()
    assert mock.await_args.args[0].id == task.id


async def test_fresh_trio_parent_is_left_alone(session, monkeypatch) -> None:
    # Recently updated → still progressing → watchdog must not touch it.
    await _seed_task(session, status=TaskStatus.TRIO_EXECUTING, age_minutes=2)
    mock = await _run_tick(session, monkeypatch)
    mock.assert_not_awaited()


async def test_stalled_freeform_design_gate_is_refired(session, monkeypatch) -> None:
    # The freeform standin-never-fired deadlock: re-invoking the driver re-fires the gate.
    task = await _seed_task(
        session, status=TaskStatus.AWAITING_DESIGN_APPROVAL, age_minutes=120, freeform=True
    )
    mock = await _run_tick(session, monkeypatch)
    mock.assert_awaited_once()
    assert mock.await_args.args[0].id == task.id


async def test_human_design_gate_is_not_failed_or_refired(session, monkeypatch) -> None:
    # A non-freeform task awaiting a human verdict is legitimately waiting — leave it.
    await _seed_task(
        session, status=TaskStatus.AWAITING_DESIGN_APPROVAL, age_minutes=120, freeform=False
    )
    mock = await _run_tick(session, monkeypatch)
    mock.assert_not_awaited()


async def test_non_trio_task_is_ignored(session, monkeypatch) -> None:
    await _seed_task(session, status=TaskStatus.CODING, age_minutes=120)
    mock = await _run_tick(session, monkeypatch)
    mock.assert_not_awaited()


async def test_stalled_parent_handled_once_until_it_progresses(session, monkeypatch) -> None:
    # Without dedup the watchdog re-notified AND re-dispatched every stale parent
    # every 5-min tick — in prod, 28 orphaned parents = a telegram each, every
    # tick, forever. Handle each stall episode once; only fresh progress re-arms.
    import agent.lifecycle.trio.recovery as recovery_mod

    recovery_mod._stall_handled.clear()
    task = await _seed_task(session, status=TaskStatus.TRIO_EXECUTING, age_minutes=120)

    first = await _run_tick(session, monkeypatch)
    first.assert_awaited_once()

    second = await _run_tick(session, monkeypatch)  # same episode, updated_at unchanged
    second.assert_not_awaited()

    # Real progress bumps updated_at → a new episode is eligible again.
    task.updated_at = datetime.now(UTC) - timedelta(minutes=120)
    await session.flush()
    third = await _run_tick(session, monkeypatch)
    third.assert_awaited_once()
