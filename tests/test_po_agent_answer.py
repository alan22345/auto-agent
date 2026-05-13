"""po_agent.answer_architect_question — writes answer + publishes RESOLVED."""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.events import TaskEventType


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_answer_writes_answer_and_publishes_resolved(session, publisher):
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
    slug = f"po-{uuid.uuid4().hex[:8]}"
    org = Organization(name=slug, slug=slug, plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(
        name="r", url="https://github.com/o/r.git",
        organization_id=org.id, default_branch="main",
        product_brief="# Mission\nBuild a TODO app for parents.",
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
        clarification_question="Should we support shared family lists?",
        session_blob_path=f"trio-{parent.id}.json",
    ))
    await session.commit()

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=MagicMock(
        output='```json\n{"answer": "Yes — shared lists are the headline feature."}\n```'
    ))

    with patch("agent.po_agent.create_agent", return_value=fake_agent), \
         patch("agent.po_agent.clone_repo",
               AsyncMock(return_value=MagicMock(root="/tmp/po-ws"))):
        from agent.po_agent import answer_architect_question
        await answer_architect_question(parent.id)

    # Answer written to the attempt row.
    from sqlalchemy import select as _sel
    attempt = (await session.execute(
        _sel(ArchitectAttempt).where(ArchitectAttempt.task_id == parent.id)
    )).scalar_one()
    assert "shared lists" in (attempt.clarification_answer or "")
    assert attempt.clarification_source == "po"

    # PO's prompt must contain product_brief.
    prompt = fake_agent.run.call_args.args[0]
    assert "Build a TODO app for parents." in prompt

    # *_RESOLVED published.
    resolved = [e for e in publisher.events
                if e.type == TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED]
    assert len(resolved) == 1
    assert resolved[0].task_id == parent.id


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_answer_handles_malformed_json(session, publisher):
    """When PO returns un-parseable JSON, the error is stored as the answer."""
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
    slug = f"poe-{uuid.uuid4().hex[:8]}"
    org = Organization(name=slug, slug=slug, plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(name="r", url="https://github.com/o/r.git",
                organization_id=org.id, default_branch="main")
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
        clarification_question="Q?",
        session_blob_path=f"trio-{parent.id}.json",
    ))
    await session.commit()

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=MagicMock(
        output="No JSON here, just prose."
    ))

    with patch("agent.po_agent.create_agent", return_value=fake_agent), \
         patch("agent.po_agent.clone_repo",
               AsyncMock(return_value=MagicMock(root="/tmp/po-ws"))):
        from agent.po_agent import answer_architect_question
        await answer_architect_question(parent.id)

    from sqlalchemy import select as _sel
    attempt = (await session.execute(
        _sel(ArchitectAttempt).where(ArchitectAttempt.task_id == parent.id)
    )).scalar_one()
    assert attempt.clarification_answer is not None
    assert "could not parse" in attempt.clarification_answer.lower() or \
           "no parseable" in attempt.clarification_answer.lower()
    assert attempt.clarification_source == "po"
