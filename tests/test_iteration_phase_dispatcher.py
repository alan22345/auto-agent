"""ADR-017 — run_trio_parent(iteration_context=…) routes to architect.iterate
and then proceeds through the existing per-item loop. After the loop, it
transitions ITERATING → AWAITING_REVIEW and publishes task_iteration_complete.
It must NOT open a second integration PR (the existing one already covers
the new commits the per-item loop pushed)."""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from shared.events import TaskEventType


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_run_trio_parent_iteration_routes_to_iterate_and_closes_loop(
    session,
    publisher,
):
    from agent.lifecycle.trio import architect, run_trio_parent
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
    org = Organization(name=f"r-{suffix}", slug=f"r-{suffix}", plan_id=plan.id)
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
        status=TaskStatus.ITERATING,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id,
        organization_id=org.id,
        trio_backlog=[],
    )
    session.add(parent)
    await session.commit()

    iterate_mock = AsyncMock()
    open_pr_mock = AsyncMock()
    with (
        patch.object(architect, "iterate", iterate_mock),
        patch(
            "agent.lifecycle.trio._open_integration_pr_and_transition",
            open_pr_mock,
        ),
    ):
        await run_trio_parent(
            parent,
            iteration_context={"feedback": "tweak it", "pr_url": "https://x/pr/1"},
        )

    iterate_mock.assert_awaited_once()
    assert iterate_mock.await_args.args[0] == parent.id
    open_pr_mock.assert_not_awaited()

    await session.refresh(parent)
    assert parent.status == TaskStatus.AWAITING_REVIEW
    assert any(
        e.type == TaskEventType.ITERATION_COMPLETE and e.task_id == parent.id
        for e in publisher.events
    )
