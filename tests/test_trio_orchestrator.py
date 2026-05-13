"""Tests for ``agent.lifecycle.trio.run_trio_parent``.

Same shape as the other trio tests in this suite: real DB session,
``async_session`` patched to a factory that yields the test's
transaction-wrapped session so every write rolls back cleanly. The
agent-side seams (``architect.run_initial``, ``architect.checkpoint``,
``scheduler.dispatch_next``, ``scheduler.await_child``,
``_open_integration_pr``) are stubbed; the orchestrator itself drives
the flow and persists status transitions against the real schema.
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import inspect, select

import agent.lifecycle.trio as trio
from agent.lifecycle.trio import architect as architect_mod
from agent.lifecycle.trio import run_trio_parent
from agent.lifecycle.trio import scheduler as scheduler_mod
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


async def _seed_parent(session) -> Task:
    org = await _seed_org(session)
    # NOTE: complexity is COMPLEX (not COMPLEX_LARGE) for local-DB compatibility
    # — the orchestrator doesn't branch on complexity, so the choice is
    # immaterial to what's under test here. test_trio_scheduler uses
    # COMPLEX_LARGE and will skip on DBs that haven't had the 012 enum value
    # backfilled; we deliberately stay one step out of that footgun.
    t = Task(
        title="Build app",
        description="Build app",
        source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING,
        complexity=TaskComplexity.COMPLEX,
        trio_phase=None,
        organization_id=org.id,
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
    real_session.refresh = AsyncMock()

    @asynccontextmanager
    async def _factory():
        yield real_session

    return _factory


# ---------------------------------------------------------------------------
# Happy path: architect → 1 child (DONE) → checkpoint(done) → final PR.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_trio_parent_drives_phases_in_order_and_opens_final_pr(
    session, monkeypatch,
):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session)
    parent_id = parent.id

    factory = _patched_async_session(session)
    monkeypatch.setattr(trio, "async_session", factory)

    phase_log: list[TrioPhase | None] = []

    real_set_phase = trio._set_trio_phase

    async def _spy_set_phase(pid, phase):
        phase_log.append(phase)
        await real_set_phase(pid, phase)

    async def fake_initial(parent_task_id):
        # Architect produces a single-item backlog.
        p = (
            await session.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        p.trio_backlog = [
            {"id": "w1", "title": "x", "description": "x", "status": "pending"},
        ]
        await session.flush()

    async def fake_dispatch(p):
        child = Task(
            title="x", description="x",
            source=TaskSource.MANUAL,
            status=TaskStatus.DONE,
            complexity=TaskComplexity.COMPLEX,
            parent_task_id=p.id,
            organization_id=p.organization_id,
        )
        session.add(child)
        await session.flush()
        return child

    async def fake_await(p, ch):
        return ch

    async def fake_checkpoint(parent_task_id, **kwargs):
        # Mark the only item done — drains the backlog.
        p = (
            await session.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        backlog = list(p.trio_backlog or [])
        if backlog:
            backlog[0]["status"] = "done"
            p.trio_backlog = backlog
            await session.flush()
        return {"action": "done"}

    async def fake_open_pr(p, target_branch):
        # Verify the orchestrator picked the right target (no repo → main).
        assert target_branch == "main"
        return "https://github.com/x/y/pull/42"

    with (
        patch.object(trio, "_set_trio_phase", new=_spy_set_phase),
        patch.object(architect_mod, "run_initial", new=fake_initial),
        patch.object(scheduler_mod, "dispatch_next", new=fake_dispatch),
        patch.object(scheduler_mod, "await_child", new=fake_await),
        patch.object(architect_mod, "checkpoint", new=fake_checkpoint),
        patch.object(trio, "_open_integration_pr", new=fake_open_pr),
    ):
        await run_trio_parent(parent)

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.PR_CREATED
    assert refreshed.pr_url == "https://github.com/x/y/pull/42"
    assert refreshed.trio_phase is None

    # Phase order: ARCHITECTING → AWAITING_BUILDER → ARCHITECT_CHECKPOINT
    # → None (cleared at end). Note: checkpoint(action="done") does NOT
    # trigger another phase set before the loop breaks.
    assert phase_log == [
        TrioPhase.ARCHITECTING,
        TrioPhase.AWAITING_BUILDER,
        TrioPhase.ARCHITECT_CHECKPOINT,
    ]


# ---------------------------------------------------------------------------
# Failure path: child terminates BLOCKED → parent BLOCKED, no PR opened.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_trio_parent_blocks_on_failed_child(session, monkeypatch):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session)
    parent_id = parent.id

    factory = _patched_async_session(session)
    monkeypatch.setattr(trio, "async_session", factory)

    async def fake_initial(parent_task_id):
        p = (
            await session.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        p.trio_backlog = [
            {"id": "w1", "title": "x", "description": "x", "status": "pending"},
        ]
        await session.flush()

    async def fake_dispatch(p):
        child = Task(
            title="x", description="x",
            source=TaskSource.MANUAL,
            status=TaskStatus.BLOCKED,
            complexity=TaskComplexity.COMPLEX,
            parent_task_id=p.id,
            organization_id=p.organization_id,
        )
        session.add(child)
        await session.flush()
        return child

    async def fake_await(p, ch):
        return ch

    open_pr_called = False

    async def fake_open_pr(p, target_branch):
        nonlocal open_pr_called
        open_pr_called = True
        return "https://example/pr/should-not-open"

    with (
        patch.object(architect_mod, "run_initial", new=fake_initial),
        patch.object(scheduler_mod, "dispatch_next", new=fake_dispatch),
        patch.object(scheduler_mod, "await_child", new=fake_await),
        patch.object(trio, "_open_integration_pr", new=fake_open_pr),
    ):
        await run_trio_parent(parent)

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.BLOCKED
    assert refreshed.pr_url is None
    assert refreshed.trio_phase is None
    assert open_pr_called is False


# ---------------------------------------------------------------------------
# Decision "revise" loops back into a revision pass before the next dispatch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_trio_parent_revise_runs_revision_then_continues(
    session, monkeypatch,
):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session)
    parent_id = parent.id

    factory = _patched_async_session(session)
    monkeypatch.setattr(trio, "async_session", factory)

    async def fake_initial(parent_task_id):
        p = (
            await session.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        p.trio_backlog = [
            {"id": "w1", "title": "first", "description": "first", "status": "pending"},
        ]
        await session.flush()

    dispatch_counter = {"n": 0}

    async def fake_dispatch(p):
        dispatch_counter["n"] += 1
        child = Task(
            title=f"c{dispatch_counter['n']}", description="x",
            source=TaskSource.MANUAL,
            status=TaskStatus.DONE,
            complexity=TaskComplexity.COMPLEX,
            parent_task_id=p.id,
            organization_id=p.organization_id,
        )
        session.add(child)
        await session.flush()
        return child

    async def fake_await(p, ch):
        return ch

    revision_called = {"n": 0}

    async def fake_revision(parent_task_id):
        # Revision rewrites the backlog — replace the (already in-progress)
        # first item with a fresh pending one.
        revision_called["n"] += 1
        p = (
            await session.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        p.trio_backlog = [
            {"id": "w2", "title": "post-rev", "description": "post-rev", "status": "pending"},
        ]
        await session.flush()

    checkpoint_counter = {"n": 0}

    async def fake_checkpoint(parent_task_id, **kwargs):
        checkpoint_counter["n"] += 1
        if checkpoint_counter["n"] == 1:
            # First child merge: ask for a revision.
            return {"action": "revise"}
        # Second child merge: backlog drained — done.
        p = (
            await session.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        backlog = list(p.trio_backlog or [])
        for item in backlog:
            if item.get("status") == "in_progress":
                item["status"] = "done"
        p.trio_backlog = backlog
        await session.flush()
        return {"action": "done"}

    async def fake_open_pr(p, target_branch):
        return "https://github.com/x/y/pull/99"

    with (
        patch.object(architect_mod, "run_initial", new=fake_initial),
        patch.object(architect_mod, "run_revision", new=fake_revision),
        patch.object(scheduler_mod, "dispatch_next", new=fake_dispatch),
        patch.object(scheduler_mod, "await_child", new=fake_await),
        patch.object(architect_mod, "checkpoint", new=fake_checkpoint),
        patch.object(trio, "_open_integration_pr", new=fake_open_pr),
    ):
        await run_trio_parent(parent)

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.PR_CREATED
    assert revision_called["n"] == 1
    assert dispatch_counter["n"] == 2
    assert checkpoint_counter["n"] == 2


# ---------------------------------------------------------------------------
# Decision "blocked" from checkpoint → parent BLOCKED, no PR opened.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_trio_parent_blocks_on_checkpoint_blocked_decision(
    session, monkeypatch,
):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session)
    parent_id = parent.id

    factory = _patched_async_session(session)
    monkeypatch.setattr(trio, "async_session", factory)

    async def fake_initial(parent_task_id):
        p = (
            await session.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        p.trio_backlog = [
            {"id": "w1", "title": "x", "description": "x", "status": "pending"},
        ]
        await session.flush()

    async def fake_dispatch(p):
        child = Task(
            title="x", description="x",
            source=TaskSource.MANUAL,
            status=TaskStatus.DONE,
            complexity=TaskComplexity.COMPLEX,
            parent_task_id=p.id,
            organization_id=p.organization_id,
        )
        session.add(child)
        await session.flush()
        return child

    async def fake_await(p, ch):
        return ch

    async def fake_checkpoint(parent_task_id, **kwargs):
        return {"action": "blocked", "reason": "design dead-end"}

    open_pr_called = False

    async def fake_open_pr(p, target_branch):
        nonlocal open_pr_called
        open_pr_called = True
        return "should-not-open"

    with (
        patch.object(architect_mod, "run_initial", new=fake_initial),
        patch.object(scheduler_mod, "dispatch_next", new=fake_dispatch),
        patch.object(scheduler_mod, "await_child", new=fake_await),
        patch.object(architect_mod, "checkpoint", new=fake_checkpoint),
        patch.object(trio, "_open_integration_pr", new=fake_open_pr),
    ):
        await run_trio_parent(parent)

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.BLOCKED
    assert refreshed.trio_phase is None
    assert open_pr_called is False


# ---------------------------------------------------------------------------
# Re-entry path: repair_context routes to checkpoint instead of run_initial.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_trio_parent_repair_context_invokes_checkpoint_first(
    session, monkeypatch,
):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session)
    parent_id = parent.id

    factory = _patched_async_session(session)
    monkeypatch.setattr(trio, "async_session", factory)

    initial_called = False

    async def fake_initial(parent_task_id):
        nonlocal initial_called
        initial_called = True

    checkpoint_kwargs_seen: dict = {}

    async def fake_checkpoint(parent_task_id, **kwargs):
        # First call carries the repair_context; subsequent calls won't.
        if "repair_context" in kwargs and not checkpoint_kwargs_seen:
            checkpoint_kwargs_seen.update(kwargs)
            # Seed a backlog so the loop has work to do.
            p = (
                await session.execute(select(Task).where(Task.id == parent_task_id))
            ).scalar_one()
            p.trio_backlog = [
                {"id": "fix1", "title": "fix", "description": "fix", "status": "pending"},
            ]
            await session.flush()
            return {"action": "continue"}
        # Post-child checkpoint: mark backlog done.
        p = (
            await session.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        backlog = list(p.trio_backlog or [])
        for item in backlog:
            if item.get("status") == "in_progress":
                item["status"] = "done"
        p.trio_backlog = backlog
        await session.flush()
        return {"action": "done"}

    async def fake_dispatch(p):
        child = Task(
            title="fix", description="fix",
            source=TaskSource.MANUAL,
            status=TaskStatus.DONE,
            complexity=TaskComplexity.COMPLEX,
            parent_task_id=p.id,
            organization_id=p.organization_id,
        )
        session.add(child)
        await session.flush()
        return child

    async def fake_await(p, ch):
        return ch

    async def fake_open_pr(p, target_branch):
        return "https://github.com/x/y/pull/7"

    repair = {"ci_log": "boom", "failed_pr_url": "https://github.com/x/y/pull/6"}

    with (
        patch.object(architect_mod, "run_initial", new=fake_initial),
        patch.object(architect_mod, "checkpoint", new=fake_checkpoint),
        patch.object(scheduler_mod, "dispatch_next", new=fake_dispatch),
        patch.object(scheduler_mod, "await_child", new=fake_await),
        patch.object(trio, "_open_integration_pr", new=fake_open_pr),
    ):
        await run_trio_parent(parent, repair_context=repair)

    assert initial_called is False
    assert checkpoint_kwargs_seen.get("repair_context") == repair

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.PR_CREATED


# ---------------------------------------------------------------------------
# After run_initial blocks the parent (invalid JSON path), we bail without
# entering the dispatch loop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_trio_parent_returns_early_if_initial_blocked_parent(
    session, monkeypatch,
):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session)
    parent_id = parent.id

    factory = _patched_async_session(session)
    monkeypatch.setattr(trio, "async_session", factory)

    async def fake_initial(parent_task_id):
        # Simulate run_initial's "invalid JSON" path: parent transitions
        # to BLOCKED, backlog stays empty.
        from orchestrator.state_machine import transition

        p = (
            await session.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        await transition(session, p, TaskStatus.BLOCKED, message="bad json")
        await session.flush()

    dispatch_called = False

    async def fake_dispatch(p):
        nonlocal dispatch_called
        dispatch_called = True
        return None

    open_pr_called = False

    async def fake_open_pr(p, target_branch):
        nonlocal open_pr_called
        open_pr_called = True
        return ""

    with (
        patch.object(architect_mod, "run_initial", new=fake_initial),
        patch.object(scheduler_mod, "dispatch_next", new=fake_dispatch),
        patch.object(trio, "_open_integration_pr", new=fake_open_pr),
    ):
        await run_trio_parent(parent)

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.BLOCKED
    assert dispatch_called is False
    assert open_pr_called is False
