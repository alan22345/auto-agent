"""Phase 7.5 — orchestrator hook for AWAITING_DESIGN_APPROVAL.

When a user POSTs ``/api/tasks/{id}/approve-plan`` for a task in
``AWAITING_DESIGN_APPROVAL`` with verdict=approved, the endpoint must:

1. Write ``.auto-agent/plan_approval.json`` (existing behaviour).
2. Transition the task to ``ARCHITECT_BACKLOG_EMIT`` (existing).
3. Publish a ``task.design_approved`` event (NEW — Phase 7.5).

The orchestrator's ``on_design_approved`` handler then re-invokes
``run_trio_parent`` so the architect's backlog-emit step fires without
the user needing to kick the task by hand.

This file pins:
  * the endpoint publishes ``task.design_approved`` only when the task
    was in ``AWAITING_DESIGN_APPROVAL`` and verdict=approved,
  * ``on_design_approved`` fires ``run_trio_parent`` for the resolved task,
  * the same endpoint does NOT publish ``task.design_approved`` on
    rejection (only on approved transitions),
  * a task already in ``AWAITING_PLAN_APPROVAL`` (complex flow) does
    NOT trigger ``task.design_approved`` (different gate).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import run as run_module
from orchestrator.router import approve_plan
from shared.events import Event, TaskEventType
from shared.models import Task, TaskStatus
from shared.types import PlanApprovalRequest


def _mock_task(*, status: TaskStatus, task_id: int = 1, org_id: int = 7):
    t = MagicMock(spec=Task)
    t.id = task_id
    t.status = status
    t.organization_id = org_id
    t.mode_override = None
    return t


@pytest.mark.asyncio
async def test_approve_design_publishes_design_approved_event(tmp_path, monkeypatch, publisher):
    """Approving a complex_large task at the design gate publishes
    ``task.design_approved`` so the orchestrator can re-enter
    ``run_trio_parent`` and emit the backlog."""

    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))

    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.add = MagicMock()
    task = _mock_task(status=TaskStatus.AWAITING_DESIGN_APPROVAL)
    # The transition mock returns the same task object — the endpoint
    # threads ``task`` through to the response builder, so we just keep
    # the reference stable.
    transition_mock = AsyncMock(return_value=task)

    with (
        patch("orchestrator.router._get_task_in_org", AsyncMock(return_value=task)),
        patch("orchestrator.router.transition", transition_mock),
        patch(
            "orchestrator.router._task_to_response",
            MagicMock(return_value=MagicMock()),
        ),
    ):
        await approve_plan(
            task_id=task.id,
            req=PlanApprovalRequest(verdict="approved", comments="LGTM"),
            session=session,
            org_id=7,
        )

    design_events = [e for e in publisher.events if e.type == TaskEventType.DESIGN_APPROVED]
    assert len(design_events) == 1, "design-approval must publish task.design_approved"
    assert design_events[0].task_id == task.id


@pytest.mark.asyncio
async def test_approve_design_rejected_does_not_publish_design_approved(
    tmp_path, monkeypatch, publisher
):
    """Rejection at the design gate must NOT publish ``task.design_approved``
    — the parent should transition to BLOCKED, not be re-queued."""

    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))

    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.add = MagicMock()
    task = _mock_task(status=TaskStatus.AWAITING_DESIGN_APPROVAL)
    transition_mock = AsyncMock(return_value=task)

    with (
        patch("orchestrator.router._get_task_in_org", AsyncMock(return_value=task)),
        patch("orchestrator.router.transition", transition_mock),
        patch(
            "orchestrator.router._task_to_response",
            MagicMock(return_value=MagicMock()),
        ),
    ):
        await approve_plan(
            task_id=task.id,
            req=PlanApprovalRequest(
                verdict="rejected",
                comments="Stack choice wrong.",
            ),
            session=session,
            org_id=7,
        )

    design_events = [e for e in publisher.events if e.type == TaskEventType.DESIGN_APPROVED]
    assert design_events == [], "rejection must not publish task.design_approved"


@pytest.mark.asyncio
async def test_approve_plan_approval_does_not_publish_design_approved(
    tmp_path, monkeypatch, publisher
):
    """Plan-approval (complex flow) is a different gate. Approving it must
    NOT publish ``task.design_approved``."""

    monkeypatch.setattr("agent.workspace.WORKSPACES_DIR", str(tmp_path))

    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.add = MagicMock()
    task = _mock_task(status=TaskStatus.AWAITING_PLAN_APPROVAL)

    with (
        patch("orchestrator.router._get_task_in_org", AsyncMock(return_value=task)),
        patch("orchestrator.router.transition", AsyncMock(return_value=task)),
        patch(
            "orchestrator.router._task_to_response",
            MagicMock(return_value=MagicMock()),
        ),
    ):
        await approve_plan(
            task_id=task.id,
            req=PlanApprovalRequest(verdict="approved", comments=""),
            session=session,
            org_id=7,
        )

    design_events = [e for e in publisher.events if e.type == TaskEventType.DESIGN_APPROVED]
    assert design_events == [], (
        "plan_approval (complex flow) must not trigger the design-approved hook"
    )


# ---------------------------------------------------------------------------
# Handler — picks up the event and re-invokes run_trio_parent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_design_approved_handler_invokes_run_trio_parent():
    """The orchestrator-side handler must dispatch ``run_trio_parent`` for
    the resolved task. The handler is fire-and-forget; we verify it was
    called with the task that the event references."""

    import asyncio

    fake_task = MagicMock()
    fake_task.id = 99
    fake_task.status = TaskStatus.ARCHITECT_BACKLOG_EMIT

    get_task_mock = AsyncMock(return_value=fake_task)
    run_trio_mock = AsyncMock()

    event = Event(type=TaskEventType.DESIGN_APPROVED, task_id=99)

    @asynccontextmanager
    async def fake_session():
        yield MagicMock()

    with (
        patch.object(run_module, "async_session", fake_session),
        patch.object(run_module, "get_task", get_task_mock),
        patch("agent.lifecycle.trio.run_trio_parent", run_trio_mock),
    ):
        await run_module.on_design_approved(event)
        # The handler uses asyncio.create_task — yield to the event loop
        # so the scheduled task can run before we assert.
        await asyncio.sleep(0)

    get_task_mock.assert_awaited_once()
    run_trio_mock.assert_awaited_once()
    args, _ = run_trio_mock.call_args
    assert args[0] is fake_task


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
