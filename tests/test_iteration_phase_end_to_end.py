"""ADR-017 — end-to-end: web UI message on an AWAITING_REVIEW trio task
threads through to architect.iterate and back to AWAITING_REVIEW with the
iteration-complete event published."""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from shared.events import HumanEventType, TaskEventType


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_feedback_flows_from_web_to_iteration_complete(session, publisher):
    from agent.lifecycle import conversation
    from agent.lifecycle.trio import architect
    from orchestrator.router import TaskMessageRequest, add_task_message
    from shared.models import (
        Organization,
        Repo,
        Task,
        TaskComplexity,
        TaskSource,
        TaskStatus,
    )
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"e2e-{suffix}", slug=f"e2e-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(
        name=f"r-{suffix}",
        url="https://github.com/o/r.git",
        organization_id=org.id,
        default_branch="main",
    )
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P",
        description="d",
        source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_REVIEW,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id,
        organization_id=org.id,
        pr_url="https://github.com/o/r/pull/1",
        trio_backlog=[],
    )
    session.add(parent)
    await session.commit()

    # 1. POST /tasks/{id}/message publishes human.message.
    with patch("orchestrator.router._get_task_in_org", return_value=parent):
        await add_task_message(
            task_id=parent.id,
            req=TaskMessageRequest(message="break it down further", username="alan"),
            session=session,
            org_id=org.id,
        )

    # 2. Simulate the bus dispatching human.message to route_human_message.
    iterate_mock = AsyncMock()
    with patch.object(architect, "iterate", iterate_mock):
        for ev in list(publisher.events):
            if ev.type == HumanEventType.MESSAGE and ev.task_id == parent.id:
                await conversation.route_human_message(ev)

    iterate_mock.assert_awaited()
    await session.refresh(parent)
    assert parent.status == TaskStatus.AWAITING_REVIEW, (
        f"end-state should be AWAITING_REVIEW, got {parent.status}"
    )
    assert any(e.type == TaskEventType.ITERATION_COMPLETE for e in publisher.events)
