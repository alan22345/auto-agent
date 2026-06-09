"""The dispatcher gate: while the health lease is held, only the loop's own
fix tasks may start; everything else is blocked."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator import queue


def _task(*, source_id="", repo_id=None, org_id=1):
    return SimpleNamespace(id=1, source_id=source_id, repo_id=repo_id, organization_id=org_id)


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
