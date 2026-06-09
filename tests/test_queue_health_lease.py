"""The dispatcher gate: while the health lease is held, only the loop's own
fix tasks may start; everything else is blocked."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator import queue


def _task(*, source_id="", repo_id=None, org_id=1, task_id=1):
    return SimpleNamespace(id=task_id, source_id=source_id, repo_id=repo_id, organization_id=org_id)


def _all_result(rows):
    """Result whose .all() returns the given list of (col,) tuples."""
    r = MagicMock()
    r.all = MagicMock(return_value=rows)
    return r


def _scalars_result(rows):
    """Result whose .scalars() iterates the given task rows."""
    r = MagicMock()
    r.scalars = MagicMock(return_value=iter(rows))
    return r


def test_is_health_loop_task_recognizes_fix_tasks():
    assert queue.is_health_loop_task(_task(source_id="health:42:batch:abc")) is True
    assert queue.is_health_loop_task(_task(source_id="health-loop")) is True
    assert queue.is_health_loop_task(_task(source_id="slack:123")) is False
    assert queue.is_health_loop_task(_task(source_id="")) is False


@pytest.mark.asyncio
async def test_can_start_blocks_non_health_task_when_lease_held():
    task = _task(source_id="slack:123")
    with (
        patch.object(queue, "count_active", AsyncMock(return_value=0)),
        patch.object(queue, "_org_at_concurrency_cap", AsyncMock(return_value=False)),
        patch.object(queue, "_repo_has_active_task", AsyncMock(return_value=False)),
        patch.object(queue, "lease_held", AsyncMock(return_value=True)),
    ):
        assert await queue.can_start_task(session=None, task=task) is False


@pytest.mark.asyncio
async def test_can_start_allows_health_fix_task_when_lease_held():
    task = _task(source_id="health:42:batch:abc")
    with (
        patch.object(queue, "count_active", AsyncMock(return_value=0)),
        patch.object(queue, "_org_at_concurrency_cap", AsyncMock(return_value=False)),
        patch.object(queue, "_repo_has_active_task", AsyncMock(return_value=False)),
        patch.object(queue, "lease_held", AsyncMock(return_value=True)),
    ):
        assert await queue.can_start_task(session=None, task=task) is True


@pytest.mark.asyncio
async def test_can_start_unaffected_when_lease_free():
    task = _task(source_id="slack:123")
    with (
        patch.object(queue, "count_active", AsyncMock(return_value=0)),
        patch.object(queue, "_org_at_concurrency_cap", AsyncMock(return_value=False)),
        patch.object(queue, "_repo_has_active_task", AsyncMock(return_value=False)),
        patch.object(queue, "lease_held", AsyncMock(return_value=False)),
    ):
        assert await queue.can_start_task(session=None, task=task) is True


@pytest.mark.asyncio
async def test_can_start_fails_open_when_lease_check_errors():
    """Fix 1: a Redis hiccup in the lease check must NOT wedge dispatch.

    If ``lease_held`` raises (e.g. Redis down), the gate fails OPEN — the
    normal (non-health) task is allowed to start rather than propagating the
    exception into the dispatch hot path.
    """
    task = _task(source_id="slack:123")
    with (
        patch.object(queue, "count_active", AsyncMock(return_value=0)),
        patch.object(queue, "_org_at_concurrency_cap", AsyncMock(return_value=False)),
        patch.object(queue, "_repo_has_active_task", AsyncMock(return_value=False)),
        patch.object(queue, "lease_held", AsyncMock(side_effect=ConnectionError("redis down"))),
    ):
        # Must not raise; must allow the task (lease treated as free).
        assert await queue.can_start_task(session=None, task=task) is True


@pytest.mark.asyncio
async def test_next_eligible_prefers_health_task_when_lease_held():
    """Fix 2: exercise the hot-path scanner's lease branch.

    With the lease held, ``next_eligible_task`` must SKIP a normal QUEUED
    task and return a ``health:``-prefixed one. Drives the real
    ``next_eligible_task`` over a canned result set (the same mock-session
    style as ``tests/test_queue_multi_tenant.py``); DATABASE_URL is unset in
    CI, so the real ``session`` fixture would skip — this keeps the most
    operationally-important branch covered unconditionally.
    """
    normal = _task(task_id=2, source_id="slack:123", repo_id=None, org_id=None)
    health = _task(task_id=3, source_id="health:42:batch:abc", repo_id=None, org_id=None)
    session = AsyncMock()
    # Call order inside next_eligible_task after count_active is patched out:
    #   1. distinct active repo_ids → .all()
    #   2. queued tasks (priority asc) → .scalars()
    session.execute = AsyncMock(
        side_effect=[
            _all_result([]),  # no busy repos
            _scalars_result([normal, health]),  # normal is head-of-line
        ]
    )
    with (
        patch.object(queue, "count_active", AsyncMock(return_value=0)),
        patch.object(queue, "_org_at_concurrency_cap", AsyncMock(return_value=False)),
        patch.object(queue, "lease_held", AsyncMock(return_value=True)),
    ):
        chosen = await queue.next_eligible_task(session)
    assert chosen is health
    assert chosen.source_id == "health:42:batch:abc"
