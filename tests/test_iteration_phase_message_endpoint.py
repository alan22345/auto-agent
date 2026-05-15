"""ADR-017 — POST /tasks/{id}/message (singular) must publish human.message
so user feedback from the legacy web UI reaches route_human_message."""

from __future__ import annotations

import os
import uuid
from unittest.mock import patch

import pytest

from shared.events import HumanEventType


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_post_singular_message_publishes_human_message(session, publisher):
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
    org = Organization(name=f"adr17-{suffix}", slug=f"adr17-{suffix}", plan_id=plan.id)
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
    task = Task(
        title="T",
        description="d",
        source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_REVIEW,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id,
        organization_id=org.id,
    )
    session.add(task)
    await session.commit()

    from orchestrator.router import add_task_message
    from shared.types import TaskMessageRequest

    with patch("orchestrator.router._get_task_in_org", return_value=task):
        await add_task_message(
            task_id=task.id,
            req=TaskMessageRequest(message="break it down further", username="alan"),
            session=session,
            org_id=org.id,
        )

    matches = [
        e for e in publisher.events if e.type == HumanEventType.MESSAGE and e.task_id == task.id
    ]
    assert len(matches) == 1, f"expected 1 human.message event, got {len(matches)}"
    assert matches[0].payload["message"] == "break it down further"
    assert matches[0].payload["source"] == "web"
