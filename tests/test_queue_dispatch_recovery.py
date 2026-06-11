"""Dispatch-reliability regression tests.

Two bugs let a lone ``QUEUED`` task strand forever:

1. ``_recover_stuck_tasks`` early-returned when there were no
   PLANNING/CODING/AWAITING_APPROVAL tasks, so its trailing
   ``_try_start_queued`` call never ran on a clean boot — a queued task
   with nothing else in flight never got dispatched at startup.
2. There was no periodic queue poller. ``_try_start_queued`` only ran on
   another task's CI/review/done/PO event, so a task blocked at
   classification time was never re-evaluated once a slot freed.

These tests pin the fixed behaviour: recovery always attempts a queue
dispatch, and the poller re-scans on a fixed interval.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import run as run_module
from shared.events import Event, TaskEventType
from shared.models import (
    Organization,
    Plan,
    Task,
    TaskComplexity,
    TaskSource,
    TaskStatus,
    User,
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


async def _seed_queued(session, **kw) -> Task:
    """A plain simple QUEUED task with no creator (skips the auth probe)
    and no repo (skips the per-repo cap). An org under its concurrency cap is
    seeded since ``organization_id`` is NOT NULL."""
    org = await _seed_org(session)
    t = Task(
        title="queued task",
        description="queued task",
        source=TaskSource.MANUAL,
        status=TaskStatus.QUEUED,
        complexity=TaskComplexity.SIMPLE,
        organization_id=org.id,
        created_by_user_id=None,
        **kw,
    )
    session.add(t)
    await session.flush()
    return t


def _patched_async_session(real_session):
    """Yield ``real_session`` from ``async with async_session() as s`` and
    forward ``commit`` to ``flush`` so writes stay inside the per-test
    savepoint."""
    real_session.commit = AsyncMock(side_effect=lambda: real_session.flush())
    real_session.close = AsyncMock()
    real_session.refresh = AsyncMock()

    @asynccontextmanager
    async def _factory():
        yield real_session

    return _factory


@pytest.mark.asyncio
async def test_recover_dispatches_queued_with_no_stuck_tasks(session, monkeypatch):
    """A clean boot with only a QUEUED task (no PLANNING/CODING/AWAITING_APPROVAL
    stuck tasks) must still dispatch the queued task. Before the fix the
    early-return skipped the queue scan entirely."""
    task = await _seed_queued(session)
    task_id = task.id

    monkeypatch.setattr(run_module, "async_session", _patched_async_session(session))

    await run_module._recover_stuck_tasks()

    refreshed = (
        await session.execute(select(Task).where(Task.id == task_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.CODING


@pytest.mark.asyncio
async def test_queued_dispatch_poller_calls_try_start_each_tick(monkeypatch):
    """The poller opens a session and calls ``_try_start_queued`` once per
    tick, then sleeps. We break out of the loop by making the sleep raise."""
    calls: list[int] = []

    async def _fake_try_start(session) -> None:
        calls.append(1)

    class _BreakError(Exception):
        pass

    async def _fake_sleep(_seconds) -> None:
        raise _BreakError()

    monkeypatch.setattr(run_module, "_try_start_queued", _fake_try_start)
    monkeypatch.setattr(
        run_module, "async_session", _patched_async_session(AsyncMock())
    )
    monkeypatch.setattr(run_module.asyncio, "sleep", _fake_sleep)

    with pytest.raises(_BreakError):
        await run_module.queued_dispatch_poller()

    assert calls == [1]


@pytest.mark.asyncio
async def test_poller_swallows_dispatch_errors_and_keeps_looping(monkeypatch):
    """A dispatch error in one tick must not kill the poller — it logs and
    sleeps to the next tick (so a transient DB blip can't wedge the queue)."""
    ticks: list[int] = []

    async def _boom(session) -> None:
        raise RuntimeError("transient")

    class _BreakError(Exception):
        pass

    async def _fake_sleep(_seconds) -> None:
        ticks.append(1)
        raise _BreakError()

    monkeypatch.setattr(run_module, "_try_start_queued", _boom)
    monkeypatch.setattr(
        run_module, "async_session", _patched_async_session(AsyncMock())
    )
    monkeypatch.setattr(run_module.asyncio, "sleep", _fake_sleep)

    # Reaches the sleep (i.e. the error was swallowed, not propagated).
    with pytest.raises(_BreakError):
        await run_module.queued_dispatch_poller()
    assert ticks == [1]


@pytest.mark.asyncio
async def test_start_queued_blocks_and_notifies_on_expired_creds(
    session, monkeypatch, publisher
):
    """The user-kicked start path must apply the same auth gate as the other
    dispatch paths: an owner with expired Claude credentials parks the task in
    BLOCKED_ON_AUTH and fires the reconnect nudge — instead of starting it into
    a silent, heartbeat-less hang (the task-319 stall)."""
    org = await _seed_org(session)
    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        password_hash="x",
        display_name="Owner",
        claude_auth_status="paired",
        organization_id=org.id,
    )
    session.add(user)
    await session.flush()
    task = Task(
        title="needs auth",
        description="needs auth",
        source=TaskSource.MANUAL,
        status=TaskStatus.QUEUED,
        complexity=TaskComplexity.SIMPLE,
        organization_id=org.id,
        created_by_user_id=user.id,
    )
    session.add(task)
    await session.flush()
    task_id = task.id

    monkeypatch.setattr(run_module, "async_session", _patched_async_session(session))
    monkeypatch.setattr(run_module, "can_start_task", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "orchestrator.claude_auth.resolve_home_dir",
        AsyncMock(return_value="/data/users/x"),
    )
    monkeypatch.setattr(
        "orchestrator.claude_auth.probe_credentials",
        AsyncMock(return_value="expired"),
    )

    await run_module.on_start_queued_task(
        Event(type=TaskEventType.START_QUEUED, task_id=task_id)
    )

    refreshed = (
        await session.execute(select(Task).where(Task.id == task_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.BLOCKED_ON_AUTH
    assert any(
        e.type == TaskEventType.CLAUDE_AUTH_REQUIRED and e.task_id == task_id
        for e in publisher.events
    )


def test_claude_auth_required_has_user_notification_formatters():
    """The reconnect nudge must actually reach the user: the event has to be
    registered in the Slack and Telegram formatter maps (events not in the map
    are silently dropped)."""
    from integrations.telegram import main as tg_main

    assert TaskEventType.CLAUDE_AUTH_REQUIRED in tg_main._NOTIFICATION_FORMATTERS
    tg_msg = tg_main._NOTIFICATION_FORMATTERS[TaskEventType.CLAUDE_AUTH_REQUIRED](
        {"reason": "expired"}, "Task #1", False, 1
    )
    assert "reconnect" in tg_msg.lower()

    # Slack pulls in slack_bolt/aiohttp, which may be absent in a bare local
    # venv (they ship in the prod image / CI). Assert when importable.
    try:
        from integrations.slack import main as slack_main
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"slack deps unavailable locally: {exc}")
    assert TaskEventType.CLAUDE_AUTH_REQUIRED in slack_main._NOTIFICATION_FORMATTERS
    slack_msg = slack_main._NOTIFICATION_FORMATTERS[TaskEventType.CLAUDE_AUTH_REQUIRED](
        {"reason": "expired"}, "Task #1", False, 1
    )
    assert "reconnect" in slack_msg.lower()
