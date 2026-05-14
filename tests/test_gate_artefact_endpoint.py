"""Tests for GET /api/tasks/{id}/gate-artefact — ADR-015 §2 Phase 12.

Returns the markdown body the human is being asked to approve at the
current gate. ``kind`` is resolved from task status:

- AWAITING_PLAN_APPROVAL  → ``.auto-agent/plan.md`` (complex flow)
- AWAITING_DESIGN_APPROVAL → ``.auto-agent/design.md`` (complex_large)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.router import get_gate_artefact
from shared.models import Task, TaskStatus


def _mock_task(
    *,
    task_id: int = 1,
    status: TaskStatus = TaskStatus.AWAITING_PLAN_APPROVAL,
    organization_id: int = 7,
):
    t = MagicMock(spec=Task)
    t.id = task_id
    t.status = status
    t.organization_id = organization_id
    return t


@pytest.mark.asyncio
async def test_returns_plan_md_when_awaiting_plan_approval(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))
    workspace = tmp_path / "7" / "task-1"
    (workspace / ".auto-agent").mkdir(parents=True)
    (workspace / ".auto-agent" / "plan.md").write_text("# The plan\n\nSteps.")

    session = AsyncMock(spec=AsyncSession)
    with patch(
        "orchestrator.router._get_task_in_org",
        AsyncMock(return_value=_mock_task()),
    ):
        out = await get_gate_artefact(task_id=1, session=session, org_id=7)

    assert out.kind == "plan"
    assert out.body.startswith("# The plan")
    assert out.path.endswith(".auto-agent/plan.md")


@pytest.mark.asyncio
async def test_returns_design_md_when_awaiting_design_approval(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))
    workspace = tmp_path / "7" / "task-1"
    (workspace / ".auto-agent").mkdir(parents=True)
    (workspace / ".auto-agent" / "design.md").write_text("# Design\n\nSlices.")

    session = AsyncMock(spec=AsyncSession)
    with patch(
        "orchestrator.router._get_task_in_org",
        AsyncMock(return_value=_mock_task(status=TaskStatus.AWAITING_DESIGN_APPROVAL)),
    ):
        out = await get_gate_artefact(task_id=1, session=session, org_id=7)

    assert out.kind == "design"
    assert out.body.startswith("# Design")


@pytest.mark.asyncio
async def test_returns_400_when_not_at_gate(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))

    session = AsyncMock(spec=AsyncSession)
    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=_mock_task(status=TaskStatus.CODING)),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await get_gate_artefact(task_id=1, session=session, org_id=7)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_returns_404_when_task_missing():
    session = AsyncMock(spec=AsyncSession)
    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=None),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await get_gate_artefact(task_id=999, session=session, org_id=7)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_returns_404_when_artefact_missing_on_disk(tmp_path, monkeypatch):
    """File missing on disk is a 404, not a 500 — the orchestrator
    must have written the artefact before transitioning."""
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))

    session = AsyncMock(spec=AsyncSession)
    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=_mock_task()),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await get_gate_artefact(task_id=1, session=session, org_id=7)
    assert exc.value.status_code == 404
