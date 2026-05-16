"""ADR-017 — handle_iteration_feedback transitions AWAITING_REVIEW →
ITERATING and re-enters run_trio_parent with iteration_context. Concurrent
feedback while ITERATING gets pushed to the task channel as guidance."""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_handle_feedback_transitions_and_dispatches(session, task_channel):
    from agent.lifecycle.trio import iteration
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
        status=TaskStatus.AWAITING_REVIEW,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id,
        organization_id=org.id,
        pr_url="https://github.com/o/r/pull/1",
    )
    session.add(parent)
    await session.commit()

    run_mock = AsyncMock()
    with patch("agent.lifecycle.trio.iteration.run_trio_parent", run_mock):
        await iteration.handle_iteration_feedback(parent.id, "break it down further")

    await session.refresh(parent)
    assert parent.status == TaskStatus.ITERATING
    run_mock.assert_awaited_once()
    kwargs = run_mock.await_args.kwargs
    assert kwargs["iteration_context"]["feedback"] == "break it down further"
    assert kwargs["iteration_context"]["pr_url"] == "https://github.com/o/r/pull/1"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_handle_feedback_during_iteration_pushes_guidance(session, task_channel):
    from agent.lifecycle.trio import iteration
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
        pr_url="https://github.com/o/r/pull/1",
    )
    session.add(parent)
    await session.commit()

    # Mark the task as actively iterating in the in-memory guard.
    iteration._active_iteration_tasks.add(parent.id)
    try:
        run_mock = AsyncMock()
        with patch("agent.lifecycle.trio.iteration.run_trio_parent", run_mock):
            await iteration.handle_iteration_feedback(parent.id, "also do X")
    finally:
        iteration._active_iteration_tasks.discard(parent.id)

    run_mock.assert_not_awaited()
    queued = await task_channel.channel(parent.id).pop_guidance()
    assert queued == "also do X"
