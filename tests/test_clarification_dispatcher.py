"""Dispatcher: ARCHITECT_CLARIFICATION_NEEDED routes by freeform_mode."""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from shared.events import Event, TaskEventType


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_freeform_dispatches_po_agent(session, publisher):
    from shared.models import (
        Organization,
        Repo,
        Task,
        TaskComplexity,
        TaskSource,
        TaskStatus,
        TrioPhase,
    )
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    slug = f"cdf-{uuid.uuid4().hex[:8]}"
    org = Organization(name=slug, slug=slug, plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(
        name="r", url="https://github.com/o/r.git",
        organization_id=org.id, default_branch="main",
    )
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_CLARIFICATION,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        freeform_mode=True, trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.commit()

    po_mock = AsyncMock()
    with patch("agent.po_agent.answer_architect_question", po_mock):
        from run import on_architect_clarification_needed
        await on_architect_clarification_needed(Event(
            type=TaskEventType.ARCHITECT_CLARIFICATION_NEEDED,
            task_id=parent.id,
            payload={"question": "Q?"},
        ))
        # Give the create_task a tick to run.
        import asyncio
        await asyncio.sleep(0)

    po_mock.assert_called_once_with(parent.id)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_non_freeform_republishes_clarification_needed(session, publisher):
    from shared.models import (
        Organization,
        Repo,
        Task,
        TaskComplexity,
        TaskSource,
        TaskStatus,
        TrioPhase,
    )
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    slug = f"cdn-{uuid.uuid4().hex[:8]}"
    org = Organization(name=slug, slug=slug, plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(
        name="r", url="https://github.com/o/r.git",
        organization_id=org.id, default_branch="main",
    )
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_CLARIFICATION,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        freeform_mode=False, trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.commit()

    from run import on_architect_clarification_needed
    await on_architect_clarification_needed(Event(
        type=TaskEventType.ARCHITECT_CLARIFICATION_NEEDED,
        task_id=parent.id,
        payload={"question": "Q?"},
    ))

    surfaced = [
        e for e in publisher.events
        if e.type == TaskEventType.CLARIFICATION_NEEDED
        and e.task_id == parent.id
    ]
    assert len(surfaced) == 1
    assert surfaced[0].payload["question"] == "Q?"
    assert surfaced[0].payload["phase"] == "trio_architect"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_resolved_transitions_and_resumes(session, publisher):
    from shared.models import (
        ArchitectAttempt,
        Organization,
        Repo,
        Task,
        TaskComplexity,
        TaskSource,
        TaskStatus,
        TrioPhase,
    )
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    slug = f"cdr-{uuid.uuid4().hex[:8]}"
    org = Organization(name=slug, slug=slug, plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(
        name="r", url="https://github.com/o/r.git",
        organization_id=org.id, default_branch="main",
    )
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_CLARIFICATION,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        freeform_mode=True, trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.flush()
    session.add(ArchitectAttempt(
        task_id=parent.id, phase="INITIAL", cycle=1,
        reasoning="r", tool_calls=[],
        clarification_question="Q?", clarification_answer="A.",
        clarification_source="po",
        session_blob_path=f"trio-{parent.id}.json",
    ))
    await session.commit()

    resume_mock = AsyncMock()
    with patch("agent.lifecycle.trio.architect.resume", resume_mock):
        from run import on_architect_clarification_resolved
        await on_architect_clarification_resolved(Event(
            type=TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED,
            task_id=parent.id,
        ))
        import asyncio
        await asyncio.sleep(0)

    await session.refresh(parent)
    assert parent.status == TaskStatus.TRIO_EXECUTING
    resume_mock.assert_called_once_with(parent.id)
