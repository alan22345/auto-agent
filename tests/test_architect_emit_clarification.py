"""Architect emits awaiting_clarification → session saved, state transitions,
ARCHITECT_CLARIFICATION_NEEDED published."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.events import TaskEventType


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_run_initial_handles_awaiting_clarification(session, publisher):
    """When the architect's output contains an awaiting_clarification JSON
    block, run_initial: writes the question to architect_attempts, transitions
    the parent to AWAITING_CLARIFICATION, publishes ARCHITECT_CLARIFICATION_NEEDED.
    """
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

    org = Organization(name="t", slug="t")
    session.add(org)
    await session.flush()
    repo = Repo(name="r", url="https://github.com/o/r.git",
                organization_id=org.id, default_branch="main")
    session.add(repo)
    await session.flush()
    parent = Task(
        title="Parent", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING, complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.commit()

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=MagicMock(
        output=(
            "I need to know which framework to use first.\n\n"
            '```json\n'
            '{"decision": {"action": "awaiting_clarification", '
            '"question": "Pick React or Vue?"}}\n'
            '```'
        ),
        messages=[MagicMock(role="user", content="seed")],
        api_messages=[MagicMock(role="user", content="seed")],
    ))

    with patch("agent.lifecycle.trio.architect.create_architect_agent",
               return_value=fake_agent), \
         patch("agent.lifecycle.trio.architect._prepare_parent_workspace",
               AsyncMock(return_value=MagicMock(root="/tmp/ws"))), \
         patch("agent.lifecycle.trio.architect.home_dir_for_task",
               AsyncMock(return_value=None)), \
         patch("agent.session.Session.save", AsyncMock()):
        from agent.lifecycle.trio import architect
        await architect.run_initial(parent.id)

    await session.refresh(parent)
    assert parent.status == TaskStatus.AWAITING_CLARIFICATION
    assert parent.trio_phase == TrioPhase.ARCHITECTING

    # Architect attempt row stores the question, not a backlog.
    from sqlalchemy import select as _sel
    attempt = (await session.execute(
        _sel(ArchitectAttempt).where(ArchitectAttempt.task_id == parent.id)
    )).scalar_one()
    assert attempt.clarification_question == "Pick React or Vue?"
    assert attempt.clarification_answer is None
    assert attempt.session_blob_path is not None

    # Event published.
    needed = [e for e in publisher.events
              if e.type == TaskEventType.ARCHITECT_CLARIFICATION_NEEDED]
    assert len(needed) == 1
    assert needed[0].task_id == parent.id
    assert needed[0].payload["question"] == "Pick React or Vue?"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_checkpoint_handles_awaiting_clarification(session, publisher):
    """Checkpoint pass can also emit awaiting_clarification."""
    from shared.models import (
        ArchitectAttempt, Organization, Repo, Task, TaskComplexity,
        TaskSource, TaskStatus, TrioPhase,
    )

    org = Organization(name="t2", slug="t2")
    session.add(org)
    await session.flush()
    repo = Repo(name="r2", url="https://github.com/o/r2.git",
                organization_id=org.id, default_branch="main")
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING, complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        trio_phase=TrioPhase.ARCHITECT_CHECKPOINT,
        trio_backlog=[{"id": "1", "title": "x", "description": "y",
                       "status": "done"}],
    )
    session.add(parent)
    # seed one INITIAL attempt with a commit so checkpoint has lineage
    session.add(ArchitectAttempt(
        task_id=parent.id, phase="INITIAL", cycle=1,
        reasoning="r", commit_sha="abc1234", tool_calls=[],
    ))
    await session.commit()

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=MagicMock(
        output=(
            'Reviewing the merge.\n```json\n'
            '{"decision": {"action": "awaiting_clarification", '
            '"question": "Should we add caching?"}}\n```'
        ),
        messages=[MagicMock(role="user", content="seed")],
        api_messages=[MagicMock(role="user", content="seed")],
    ))

    with patch("agent.lifecycle.trio.architect.create_architect_agent",
               return_value=fake_agent), \
         patch("agent.lifecycle.trio.architect._prepare_parent_workspace",
               AsyncMock(return_value=MagicMock(root="/tmp/ws"))), \
         patch("agent.lifecycle.trio.architect.home_dir_for_task",
               AsyncMock(return_value=None)), \
         patch("agent.session.Session.save", AsyncMock()):
        from agent.lifecycle.trio import architect
        await architect.checkpoint(parent.id, child_task_id=99)

    await session.refresh(parent)
    assert parent.status == TaskStatus.AWAITING_CLARIFICATION

    from sqlalchemy import select as _sel
    attempts = (await session.execute(
        _sel(ArchitectAttempt).where(ArchitectAttempt.task_id == parent.id)
        .order_by(ArchitectAttempt.id)
    )).scalars().all()
    # Two rows: the seeded INITIAL + a new CHECKPOINT with the question.
    assert len(attempts) == 2
    assert attempts[-1].clarification_question == "Should we add caching?"

    needed = [e for e in publisher.events
              if e.type == TaskEventType.ARCHITECT_CLARIFICATION_NEEDED]
    assert len(needed) == 1
