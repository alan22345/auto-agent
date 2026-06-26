"""DB-truth reconciler backstop — no task strands silently.

The event bus is best-effort (commit-then-publish + ack-on-exception can drop an
event). Rather than make the bus reliable, the reconciler treats the DB as the
source of truth: it sweeps non-terminal tasks and flags any that has gone quiet
past a threshold and is NOT already being re-driven by a dedicated loop. The
decision is a pure function so it can be exhaustively unit-tested with no DB.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.reconciler import RECONCILE_STALL_THRESHOLD, is_silently_stuck
from shared.models import Organization, Plan, Task, TaskStatus

NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=UTC)
STALE = NOW - RECONCILE_STALL_THRESHOLD - timedelta(minutes=1)
FRESH = NOW - timedelta(minutes=1)


def _task(status: TaskStatus, updated_at: datetime) -> Task:
    return Task(title="t", description="", status=status, updated_at=updated_at)


@pytest.mark.parametrize(
    "status",
    [
        TaskStatus.AWAITING_DESIGN_APPROVAL,
        TaskStatus.AWAITING_CLARIFICATION,
        TaskStatus.BLOCKED,
        TaskStatus.CLASSIFYING,
        TaskStatus.VERIFYING,
        TaskStatus.AWAITING_REQUIRED_SECRETS,
    ],
)
def test_stalled_uncovered_states_are_flagged(status) -> None:
    assert is_silently_stuck(_task(status, STALE), now=NOW, heartbeat_alive=False) is True


def test_fresh_task_is_not_flagged() -> None:
    task = _task(TaskStatus.AWAITING_DESIGN_APPROVAL, FRESH)
    assert is_silently_stuck(task, now=NOW, heartbeat_alive=False) is False


def test_live_heartbeat_is_not_flagged() -> None:
    # An agent actively looping (even past the threshold) is alive — never flag.
    task = _task(TaskStatus.VERIFYING, STALE)
    assert is_silently_stuck(task, now=NOW, heartbeat_alive=True) is False


@pytest.mark.parametrize("status", [TaskStatus.DONE, TaskStatus.FAILED])
def test_terminal_tasks_are_never_flagged(status) -> None:
    assert is_silently_stuck(_task(status, STALE), now=NOW, heartbeat_alive=False) is False


@pytest.mark.parametrize(
    "status",
    [TaskStatus.QUEUED, TaskStatus.PLANNING, TaskStatus.CODING],
)
def test_states_owned_by_other_loops_are_skipped(status) -> None:
    # queued_dispatch_poller / task_timeout_watchdog already re-drive these;
    # the backstop must not double-report them.
    assert is_silently_stuck(_task(status, STALE), now=NOW, heartbeat_alive=False) is False


# ---------------------------------------------------------------------------
# reconcile_once — the DB sweep (flag, exclude terminal, notify once)
# ---------------------------------------------------------------------------


def _patched_async_session(real_session):
    real_session.commit = AsyncMock(side_effect=lambda: real_session.flush())
    real_session.close = AsyncMock()

    @asynccontextmanager
    async def _factory():
        yield real_session

    return _factory


async def _seed(session, *, status: TaskStatus, age_minutes: int) -> Task:
    plan = Plan(
        name=f"p-{uuid.uuid4().hex[:6]}",
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
    t = Task(
        title="t",
        description="",
        source="manual",
        source_id=f"s-{uuid.uuid4().hex[:6]}",
        status=status,
        organization_id=org.id,
        updated_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
    )
    session.add(t)
    await session.flush()
    return t


@asynccontextmanager
async def _harness(session, monkeypatch):
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    import orchestrator.reconciler as rec

    monkeypatch.setattr(rec, "async_session", _patched_async_session(session))
    rec._surfaced.clear()
    notify = AsyncMock()
    channel = AsyncMock()
    channel.is_alive = AsyncMock(return_value=False)
    with (
        patch("shared.notifier.send_telegram_async", new=notify),
        patch("shared.task_channel.task_channel", return_value=channel),
    ):
        yield rec, notify


@pytest.mark.asyncio
async def test_sweep_flags_and_notifies_stuck_task(session, monkeypatch) -> None:
    async with _harness(session, monkeypatch) as (rec, notify):
        task = await _seed(session, status=TaskStatus.AWAITING_DESIGN_APPROVAL, age_minutes=600)
        stuck = await rec.reconcile_once()
        assert stuck == [task.id]
        notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_sweep_ignores_terminal_and_fresh(session, monkeypatch) -> None:
    async with _harness(session, monkeypatch) as (rec, notify):
        await _seed(session, status=TaskStatus.DONE, age_minutes=600)
        await _seed(session, status=TaskStatus.VERIFYING, age_minutes=1)
        stuck = await rec.reconcile_once()
        assert stuck == []
        notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_notifies_once_per_stall(session, monkeypatch) -> None:
    async with _harness(session, monkeypatch) as (rec, notify):
        await _seed(session, status=TaskStatus.BLOCKED, age_minutes=600)
        await rec.reconcile_once()
        await rec.reconcile_once()  # second sweep, same stall
        notify.assert_awaited_once()
