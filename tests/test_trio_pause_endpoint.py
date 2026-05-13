"""Tests for POST /api/tasks/{id}/pause-trio.

Uses the direct-function-call pattern from test_attempts_endpoints.py:
call the endpoint function with a mocked AsyncSession and patch
_get_task_in_org, then assert on raised HTTPException or return value.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.router import pause_trio
from shared.models import Task, TaskStatus


def _mock_task(status: TaskStatus = TaskStatus.TRIO_EXECUTING) -> MagicMock:
    t = MagicMock(spec=Task)
    t.id = 1
    t.status = status
    t.trio_phase = "architect"
    return t


@pytest.mark.asyncio
async def test_pause_endpoint_transitions_trio_parent_to_blocked():
    """Seed a TRIO_EXECUTING parent. Call pause_trio. Assert 200 + BLOCKED + trio_phase=None."""
    task = _mock_task(TaskStatus.TRIO_EXECUTING)
    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()

    transitioned_task = MagicMock(spec=Task)
    transitioned_task.status = TaskStatus.BLOCKED

    with (
        patch("orchestrator.router._get_task_in_org", AsyncMock(return_value=task)),
        patch("orchestrator.router.transition", AsyncMock(return_value=transitioned_task)) as mock_transition,
    ):
        result = await pause_trio(task_id=1, session=session, org_id=1)

    assert result == {"ok": True}
    assert task.trio_phase is None
    mock_transition.assert_awaited_once_with(session, task, TaskStatus.BLOCKED)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_pause_endpoint_rejects_non_trio_task():
    """Seed a CODING task. Call pause_trio. Assert 400."""
    task = _mock_task(TaskStatus.CODING)
    session = AsyncMock(spec=AsyncSession)

    with (
        patch("orchestrator.router._get_task_in_org", AsyncMock(return_value=task)),
        pytest.raises(HTTPException) as exc,
    ):
        await pause_trio(task_id=1, session=session, org_id=1)

    assert exc.value.status_code == 400
    assert "TRIO_EXECUTING" in exc.value.detail


@pytest.mark.asyncio
async def test_pause_endpoint_404_for_unknown_task():
    """Call pause_trio with a non-existent task id. Assert 404."""
    session = AsyncMock(spec=AsyncSession)

    with (
        patch("orchestrator.router._get_task_in_org", AsyncMock(return_value=None)),
        pytest.raises(HTTPException) as exc,
    ):
        await pause_trio(task_id=9999, session=session, org_id=1)

    assert exc.value.status_code == 404
    assert "task not found" in exc.value.detail
