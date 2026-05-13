"""Recovery on restart: AWAITING_CLARIFICATION + trio_phase set + answer
written pre-crash → re-publish RESOLVED so the architect resumes."""

from __future__ import annotations

import os
import uuid

import pytest

from shared.events import TaskEventType


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_recovery_republishes_resolved_when_answer_landed_pre_crash(
    session,
    publisher,
):
    from shared.models import (
        ArchitectAttempt,
        Organization,
        Task,
        TaskComplexity,
        TaskSource,
        TaskStatus,
        TrioPhase,
    )
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"rc-{suffix}", slug=f"rc-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    parent = Task(
        title="P",
        description="d",
        source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_CLARIFICATION,
        complexity=TaskComplexity.COMPLEX_LARGE,
        organization_id=org.id,
        freeform_mode=True,
        trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.flush()
    # Pre-crash: answer was written but resume hadn't fired yet.
    session.add(
        ArchitectAttempt(
            task_id=parent.id,
            phase="INITIAL",
            cycle=1,
            reasoning="r",
            tool_calls=[],
            clarification_question="Q?",
            clarification_answer="A.",
            clarification_source="po",
            session_blob_path=f"trio-{parent.id}.json",
        )
    )
    await session.commit()

    from agent.lifecycle.trio.recovery import resume_all_trio_parents

    await resume_all_trio_parents()

    resolved = [
        e
        for e in publisher.events
        if e.type == TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED and e.task_id == parent.id
    ]
    assert len(resolved) == 1


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_recovery_does_not_publish_when_still_waiting(session, publisher):
    """No answer yet → recovery does nothing (still waiting on a human)."""
    from shared.models import (
        ArchitectAttempt,
        Organization,
        Task,
        TaskComplexity,
        TaskSource,
        TaskStatus,
        TrioPhase,
    )
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"rcw-{suffix}", slug=f"rcw-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    parent = Task(
        title="P",
        description="d",
        source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_CLARIFICATION,
        complexity=TaskComplexity.COMPLEX_LARGE,
        organization_id=org.id,
        freeform_mode=False,
        trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.flush()
    session.add(
        ArchitectAttempt(
            task_id=parent.id,
            phase="INITIAL",
            cycle=1,
            reasoning="r",
            tool_calls=[],
            clarification_question="Q?",
            # No answer yet.
            session_blob_path=f"trio-{parent.id}.json",
        )
    )
    await session.commit()

    from agent.lifecycle.trio.recovery import resume_all_trio_parents

    await resume_all_trio_parents()

    resolved = [
        e
        for e in publisher.events
        if e.type == TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED and e.task_id == parent.id
    ]
    assert resolved == []
