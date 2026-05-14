"""Tests for POST /api/tasks/{id}/approve-plan — ADR-015 §2 / §6 Phase 12.

The endpoint writes ``.auto-agent/plan_approval.json`` to the task's
workspace, transitions the task's state machine accordingly, persists a
``GateDecision`` row with ``source="user"``, and publishes a
``standin.decision`` event so the gate-history panel can render the
verdict alongside any standin decisions.

Verdict ``"approved"`` advances the task: CODING (complex /
AWAITING_PLAN_APPROVAL) or ARCHITECT_BACKLOG_EMIT (complex_large /
AWAITING_DESIGN_APPROVAL). Verdict ``"rejected"`` parks at BLOCKED with
the user's comments captured on the TaskHistory row.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.router import approve_plan
from shared.models import Task, TaskStatus
from shared.types import PlanApprovalRequest


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
    t.mode_override = None
    return t


@pytest.mark.asyncio
async def test_approve_plan_writes_file_and_returns_task(tmp_path, monkeypatch):
    """Happy path — verdict=approved writes plan_approval.json + transitions to CODING."""
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))
    workspace = tmp_path / "7" / "task-1"
    (workspace / ".auto-agent").mkdir(parents=True, exist_ok=True)
    (workspace / ".auto-agent" / "plan.md").write_text("# Plan\nDo the thing.")

    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    task = _mock_task()

    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        patch(
            "orchestrator.router.transition",
            AsyncMock(return_value=task),
        ),
        patch(
            "orchestrator.router._task_to_response",
            MagicMock(return_value=MagicMock()),
        ),
    ):
        await approve_plan(
            task_id=1,
            req=PlanApprovalRequest(verdict="approved", comments="LGTM"),
            session=session,
            org_id=7,
        )

    approval_path = workspace / ".auto-agent" / "plan_approval.json"
    assert approval_path.exists(), "approve-plan must write plan_approval.json"
    payload = json.loads(approval_path.read_text())
    assert payload["verdict"] == "approved"
    assert payload["comments"] == "LGTM"
    assert payload["schema_version"] == "1"
    assert payload["source"] == "user"


@pytest.mark.asyncio
async def test_approve_plan_publishes_decision_event(tmp_path, monkeypatch, publisher):
    """A standin.decision event must be published with source=user."""
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))

    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.add = MagicMock()
    task = _mock_task()

    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        patch(
            "orchestrator.router.transition",
            AsyncMock(return_value=task),
        ),
        patch(
            "orchestrator.router._task_to_response",
            MagicMock(return_value=MagicMock()),
        ),
    ):
        await approve_plan(
            task_id=1,
            req=PlanApprovalRequest(verdict="approved", comments=""),
            session=session,
            org_id=7,
        )

    decision_events = [e for e in publisher.events if e.type == "standin.decision"]
    assert len(decision_events) == 1
    payload = decision_events[0].payload
    assert payload["source"] == "user"
    assert payload["gate"] == "plan_approval"
    assert payload["decision"] == "approved"
    assert payload["task_id"] == 1


@pytest.mark.asyncio
async def test_approve_plan_404_when_task_missing():
    session = AsyncMock(spec=AsyncSession)
    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=None),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await approve_plan(
            task_id=999,
            req=PlanApprovalRequest(verdict="approved"),
            session=session,
            org_id=7,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_approve_plan_rejected_transitions_to_blocked(tmp_path, monkeypatch):
    """verdict=rejected parks the task at BLOCKED with comments in history."""
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))

    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.add = MagicMock()
    task = _mock_task()
    transition_mock = AsyncMock(return_value=task)

    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        patch("orchestrator.router.transition", transition_mock),
        patch(
            "orchestrator.router._task_to_response",
            MagicMock(return_value=MagicMock()),
        ),
    ):
        await approve_plan(
            task_id=1,
            req=PlanApprovalRequest(verdict="rejected", comments="needs more depth"),
            session=session,
            org_id=7,
        )

    # The transition function was called with BLOCKED.
    args, kwargs = transition_mock.call_args
    # transition(session, task, to_status, message)
    to_status = kwargs.get("to_status", args[2] if len(args) > 2 else None)
    assert to_status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_approve_plan_400_when_task_not_in_gate_status(tmp_path, monkeypatch):
    """Only AWAITING_PLAN_APPROVAL and AWAITING_DESIGN_APPROVAL are accepted."""
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))

    session = AsyncMock(spec=AsyncSession)
    task = _mock_task(status=TaskStatus.CODING)

    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await approve_plan(
            task_id=1,
            req=PlanApprovalRequest(verdict="approved"),
            session=session,
            org_id=7,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_approve_design_advances_to_backlog_emit(tmp_path, monkeypatch):
    """For complex_large tasks (status=AWAITING_DESIGN_APPROVAL), approve
    advances to ARCHITECT_BACKLOG_EMIT — the design doc is the same artefact
    per ADR-015 §2 but the next state differs."""
    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))

    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.add = MagicMock()
    task = _mock_task(status=TaskStatus.AWAITING_DESIGN_APPROVAL)
    transition_mock = AsyncMock(return_value=task)

    with (
        patch(
            "orchestrator.router._get_task_in_org",
            AsyncMock(return_value=task),
        ),
        patch("orchestrator.router.transition", transition_mock),
        patch(
            "orchestrator.router._task_to_response",
            MagicMock(return_value=MagicMock()),
        ),
    ):
        await approve_plan(
            task_id=1,
            req=PlanApprovalRequest(verdict="approved"),
            session=session,
            org_id=7,
        )

    args, kwargs = transition_mock.call_args
    to_status = kwargs.get("to_status", args[2] if len(args) > 2 else None)
    assert to_status == TaskStatus.ARCHITECT_BACKLOG_EMIT
