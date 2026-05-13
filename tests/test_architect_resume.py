"""architect.resume loads session, injects answer, continues AgentLoop."""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_resume_continues_agent_with_answer_as_user_message(
    session, publisher,
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
    slug = f"rs-{uuid.uuid4().hex[:8]}"
    org = Organization(name=slug, slug=slug, plan_id=plan.id)
    session.add(org)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING, complexity=TaskComplexity.COMPLEX_LARGE,
        organization_id=org.id,
        trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.flush()
    session.add(ArchitectAttempt(
        task_id=parent.id, phase="INITIAL", cycle=1,
        reasoning="prior reasoning", tool_calls=[],
        clarification_question="Pick React or Vue?",
        clarification_answer="React, the team knows it.",
        clarification_source="po",
        session_blob_path=f"trio-{parent.id}.json",
    ))
    await session.commit()

    fake_agent = MagicMock()
    # Simulate the architect emitting a backlog on resume.
    fake_agent.run = AsyncMock(return_value=MagicMock(
        output=(
            'Got it, going with React.\n```json\n'
            '{"backlog": [{"id": "1", "title": "Setup React app", '
            '"description": "Scaffold create-react-app"}]}\n```'
        ),
        messages=[], api_messages=[],
    ))
    fake_agent.messages = []
    fake_agent.api_messages = []

    with patch("agent.lifecycle.trio.architect.create_architect_agent",
               return_value=fake_agent), \
         patch("agent.lifecycle.trio.architect._prepare_parent_workspace",
               AsyncMock(return_value=MagicMock(root="/tmp/ws"))), \
         patch("agent.lifecycle.trio.architect.home_dir_for_task",
               AsyncMock(return_value=None)), \
         patch("agent.session.Session.load",
               AsyncMock(return_value=([], []))), \
         patch("agent.lifecycle.trio.architect._commit_and_open_initial_pr",
               AsyncMock(return_value="deadbeef")):
        from agent.lifecycle.trio import architect
        await architect.resume(parent.id)

    # The resume prompt should reference the answer.
    call_args = fake_agent.run.call_args
    prompt = call_args.args[0] if call_args.args else call_args.kwargs.get("prompt", "")
    assert "ANSWER FROM PO" in prompt
    assert "React, the team knows it." in prompt

    # Parent should now have a backlog populated.
    await session.refresh(parent)
    assert parent.trio_backlog is not None
    assert len(parent.trio_backlog) == 1


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_resume_falls_back_when_session_blob_missing(session, publisher):
    """If Session.load returns None, resume calls run_initial with the Q&A
    appended to the task description as additional context."""
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
    slug = f"rl-{uuid.uuid4().hex[:8]}"
    org = Organization(name=slug, slug=slug, plan_id=plan.id)
    session.add(org)
    await session.flush()
    parent = Task(
        title="P", description="original description",
        source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING, complexity=TaskComplexity.COMPLEX_LARGE,
        organization_id=org.id,
        trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.flush()
    session.add(ArchitectAttempt(
        task_id=parent.id, phase="INITIAL", cycle=1,
        reasoning="prior", tool_calls=[],
        clarification_question="Q?",
        clarification_answer="A.",
        clarification_source="user",
        session_blob_path=f"trio-{parent.id}.json",
    ))
    await session.commit()

    run_initial_mock = AsyncMock()
    with patch("agent.session.Session.load", AsyncMock(return_value=None)), \
         patch("agent.lifecycle.trio.architect._prepare_parent_workspace",
               AsyncMock(return_value=MagicMock(root="/tmp/ws"))), \
         patch("agent.lifecycle.trio.architect.home_dir_for_task",
               AsyncMock(return_value=None)), \
         patch("agent.lifecycle.trio.architect.run_initial", run_initial_mock):
        from agent.lifecycle.trio import architect
        await architect.resume(parent.id)

    run_initial_mock.assert_called_once_with(parent.id)
