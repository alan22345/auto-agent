"""Tests for GET /api/tasks/{id}/gate-history — ADR-015 §6 Phase 12.

Returns every persisted gate decision (user or standin) for a task,
oldest first. The web-next audit panel renders this list verbatim.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.router import list_gate_history
from shared.models import GateDecision, Task, TaskStatus


def _mock_task(task_id: int = 1):
    t = MagicMock(spec=Task)
    t.id = task_id
    t.status = TaskStatus.AWAITING_PLAN_APPROVAL
    return t


def _mock_query_result(rows):
    result = MagicMock()
    result.scalars.return_value = MagicMock(all=MagicMock(return_value=rows))
    return result


@pytest.mark.asyncio
async def test_gate_history_returns_rows_oldest_first():
    rows = [
        GateDecision(
            id=1,
            task_id=1,
            gate="plan_approval",
            source="po_standin",
            agent_id="po_standin:42",
            verdict="approved",
            comments="LGTM",
            cited_context=["product_brief"],
            fallback_reasons=[],
            created_at=datetime.now(UTC),
        ),
        GateDecision(
            id=2,
            task_id=1,
            gate="pr_review",
            source="user",
            agent_id=None,
            verdict="approved",
            comments="ship it",
            cited_context=[],
            fallback_reasons=[],
            created_at=datetime.now(UTC),
        ),
    ]
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_mock_query_result(rows))

    with patch(
        "orchestrator.router._get_task_in_org",
        AsyncMock(return_value=_mock_task()),
    ):
        out = await list_gate_history(task_id=1, session=session, org_id=7)

    assert len(out) == 2
    assert out[0].gate == "plan_approval"
    assert out[0].source == "po_standin"
    assert out[0].agent_id == "po_standin:42"
    assert out[0].cited_context == ["product_brief"]
    assert out[1].gate == "pr_review"
    assert out[1].source == "user"
    assert out[1].agent_id is None


@pytest.mark.asyncio
async def test_gate_history_returns_empty_list_for_new_task():
    """A task with no gate decisions yet returns an empty array, not 404."""
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_mock_query_result([]))

    with patch(
        "orchestrator.router._get_task_in_org",
        AsyncMock(return_value=_mock_task()),
    ):
        out = await list_gate_history(task_id=1, session=session, org_id=7)

    assert out == []


@pytest.mark.asyncio
async def test_gate_history_404_when_task_missing():
    session = AsyncMock(spec=AsyncSession)
    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=None),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await list_gate_history(task_id=999, session=session, org_id=7)
    assert exc.value.status_code == 404
