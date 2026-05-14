"""Tests for ``agent.lifecycle.trio.run_trio_parent``.

After ADR-013 the orchestrator no longer creates child Task rows. It
drives the backlog via ``dispatcher.dispatch_item`` and opens the
integration PR when the backlog is drained. These tests mock the
seams (``architect.run_initial``, ``architect.checkpoint``,
``dispatcher.dispatch_item``, ``_prepare_parent_workspace``,
``_open_integration_pr``) and verify the orchestrator's state
transitions against a real Postgres session.

Tests skip when the local DB doesn't have the trio migration applied.
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
from agent.lifecycle.trio import dispatcher as dispatcher_mod
from agent.lifecycle.trio import run_trio_parent
from agent.lifecycle.trio.dispatcher import ItemResult
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
        return cols & {"trio_phase", "trio_backlog"}

    conn = await session.connection()
    present = await conn.run_sync(_trio_cols)
    if len(present) < 2:
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


async def _seed_parent(session, *, backlog: list[dict]) -> Task:
    org = await _seed_org(session)
    t = Task(
        title="Build app",
        description="Build app",
        source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING,
        complexity=TaskComplexity.COMPLEX,
        trio_phase=None,
        trio_backlog=backlog,
        organization_id=org.id,
    )
    session.add(t)
    await session.flush()
    return t


def _patched_async_session(real_session):
    @asynccontextmanager
    async def factory():
        class _Wrapper:
            def __init__(self, s):
                self._s = s

            async def execute(self, *a, **kw):
                return await self._s.execute(*a, **kw)

            def add(self, obj):
                self._s.add(obj)

            async def commit(self):
                await self._s.flush()

            async def flush(self):
                await self._s.flush()

        yield _Wrapper(real_session)

    return factory


@pytest.mark.asyncio
async def test_run_trio_parent_dispatches_each_item_and_opens_pr(session):
    """Happy path: 2-item backlog, each dispatch_item ok, final checkpoint
    returns done, integration PR opens."""
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(
        session,
        backlog=[
            {"id": "a", "title": "T1", "description": "D1", "status": "pending"},
            {"id": "b", "title": "T2", "description": "D2", "status": "pending"},
        ],
    )

    dispatch_calls: list[str] = []

    async def fake_dispatch_item(*, work_item, **_kw):
        dispatch_calls.append(work_item["id"])
        return ItemResult(
            ok=True, transcript=[], start_sha="s0", head_sha=f"h-{work_item['id']}",
        )

    with (
        patch.object(trio, "async_session", _patched_async_session(session)),
        patch.object(architect_mod, "run_initial", new=AsyncMock(return_value=None)),
        patch.object(
            architect_mod, "checkpoint",
            new=AsyncMock(return_value={"action": "done"}),
        ),
        patch.object(
            architect_mod, "_prepare_parent_workspace",
            new=AsyncMock(return_value="/tmp/ws"),
        ),
        patch.object(
            dispatcher_mod, "dispatch_item", new=AsyncMock(side_effect=fake_dispatch_item),
        ),
        patch.object(trio, "_open_integration_pr", new=AsyncMock(return_value="http://pr")),
        patch.object(trio, "_ensure_integration_branch_checked_out", new=AsyncMock()),
    ):
        await run_trio_parent(parent)

    assert dispatch_calls == ["a", "b"]

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent.id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.PR_CREATED
    assert refreshed.pr_url == "http://pr"
    assert [it["status"] for it in refreshed.trio_backlog] == ["done", "done"]


@pytest.mark.asyncio
async def test_terminal_failure_blocks_parent(session):
    """Dispatcher returns ok=False, needs_tiebreak=False → parent BLOCKED."""
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(
        session,
        backlog=[{"id": "a", "title": "T", "description": "D", "status": "pending"}],
    )

    bad_result = ItemResult(
        ok=False,
        transcript=[],
        start_sha="s",
        needs_tiebreak=False,
        failure_reason="coder_produced_no_diff",
    )
    with (
        patch.object(trio, "async_session", _patched_async_session(session)),
        patch.object(architect_mod, "run_initial", new=AsyncMock()),
        patch.object(
            architect_mod, "_prepare_parent_workspace",
            new=AsyncMock(return_value="/tmp/ws"),
        ),
        patch.object(
            dispatcher_mod, "dispatch_item", new=AsyncMock(return_value=bad_result),
        ),
        patch.object(trio, "_ensure_integration_branch_checked_out", new=AsyncMock()),
    ):
        await run_trio_parent(parent)

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent.id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_tiebreak_accept_marks_item_done(session):
    """Dispatcher needs tiebreak; architect returns accept → item done, loop continues."""
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(
        session,
        backlog=[{"id": "a", "title": "T", "description": "D", "status": "pending"}],
    )

    needs_tb = ItemResult(
        ok=False, transcript=[], start_sha="s", head_sha="h",
        needs_tiebreak=True,
    )

    with (
        patch.object(trio, "async_session", _patched_async_session(session)),
        patch.object(architect_mod, "run_initial", new=AsyncMock()),
        patch.object(
            architect_mod, "checkpoint",
            new=AsyncMock(return_value={"action": "done"}),
        ),
        patch.object(
            architect_mod, "_prepare_parent_workspace",
            new=AsyncMock(return_value="/tmp/ws"),
        ),
        patch.object(
            dispatcher_mod, "dispatch_item", new=AsyncMock(return_value=needs_tb),
        ),
        patch.object(
            dispatcher_mod, "architect_tiebreak",
            new=AsyncMock(return_value={"action": "accept", "reason": "spec ok"}),
        ),
        patch.object(trio, "_open_integration_pr", new=AsyncMock(return_value="")),
        patch.object(trio, "_ensure_integration_branch_checked_out", new=AsyncMock()),
    ):
        await run_trio_parent(parent)

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent.id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.PR_CREATED
    assert refreshed.trio_backlog[0]["status"] == "done"


@pytest.mark.asyncio
async def test_tiebreak_clarify_blocks_parent_with_question(session):
    """Clarify tiebreak blocks the parent (no resumable session today)."""
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(
        session,
        backlog=[{"id": "a", "title": "T", "description": "D", "status": "pending"}],
    )

    needs_tb = ItemResult(
        ok=False, transcript=[], start_sha="s", needs_tiebreak=True,
    )

    with (
        patch.object(trio, "async_session", _patched_async_session(session)),
        patch.object(architect_mod, "run_initial", new=AsyncMock()),
        patch.object(
            architect_mod, "_prepare_parent_workspace",
            new=AsyncMock(return_value="/tmp/ws"),
        ),
        patch.object(
            dispatcher_mod, "dispatch_item", new=AsyncMock(return_value=needs_tb),
        ),
        patch.object(
            dispatcher_mod, "architect_tiebreak",
            new=AsyncMock(return_value={"action": "clarify", "question": "Which stack?"}),
        ),
        patch.object(trio, "_ensure_integration_branch_checked_out", new=AsyncMock()),
    ):
        await run_trio_parent(parent)

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent.id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.BLOCKED
