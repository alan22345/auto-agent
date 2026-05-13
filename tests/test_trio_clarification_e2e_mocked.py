"""End-to-end mocked: architect asks → PO answers → architect resumes →
backlog populated. LLM provider is mocked at the provider level so all
the seams (events, state machine, session persistence) run for real."""
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
async def test_clarification_round_trip_populates_backlog(session, publisher):
    """Two architect LLM calls: first emits clarification, second emits backlog."""
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
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"e2e-{suffix}", slug=f"e2e-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(
        name=f"r-{suffix}", url="https://github.com/o/r.git",
        organization_id=org.id, default_branch="main",
        product_brief="# Build a TODO app",
    )
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="Build something cool",
        source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        freeform_mode=True, trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.commit()

    architect_outputs = iter([
        # Round 1: clarification.
        '```json\n{"decision": {"action": "awaiting_clarification", '
        '"question": "Pick framework?"}}\n```',
        # Round 2 (after answer arrives): backlog.
        '```json\n{"backlog": [{"id": "1", "title": "Init app", '
        '"description": "Scaffold"}]}\n```',
    ])

    def fake_arch_agent(*args, **kwargs):
        agent = MagicMock()
        agent.messages = []
        agent.api_messages = []
        agent.run = AsyncMock(side_effect=lambda *a, **kw: MagicMock(
            output=next(architect_outputs),
            messages=[], api_messages=[],
        ))
        return agent

    po_agent_mock = MagicMock()
    po_agent_mock.run = AsyncMock(return_value=MagicMock(
        output='```json\n{"answer": "React. Team knows it."}\n```'
    ))

    with patch("agent.lifecycle.trio.architect.create_architect_agent",
               side_effect=fake_arch_agent), \
         patch("agent.lifecycle.trio.architect._prepare_parent_workspace",
               AsyncMock(return_value=MagicMock(root="/tmp/ws-e2e"))), \
         patch("agent.lifecycle.trio.architect.home_dir_for_task",
               AsyncMock(return_value=None)), \
         patch("agent.lifecycle.trio.architect._commit_and_open_initial_pr",
               AsyncMock(return_value="cafe1234")), \
         patch("agent.session.Session.save", AsyncMock()), \
         patch("agent.session.Session.load",
               AsyncMock(return_value=([], []))), \
         patch("agent.po_agent.create_agent", return_value=po_agent_mock), \
         patch("agent.po_agent.clone_repo",
               AsyncMock(return_value=MagicMock(root="/tmp/po-ws"))):

        # 1. Architect first run — emits clarification.
        from agent.lifecycle.trio import architect
        await architect.run_initial(parent.id)

        # 2. Dispatcher fires PO (simulating the bus).
        from run import on_architect_clarification_needed
        from shared.events import Event, TaskEventType
        await on_architect_clarification_needed(Event(
            type=TaskEventType.ARCHITECT_CLARIFICATION_NEEDED,
            task_id=parent.id,
            payload={"question": "Pick framework?"},
        ))
        import asyncio
        await asyncio.sleep(0)
        # po_agent runs in a create_task — give it a tick.
        for _ in range(10):
            await session.refresh(parent)
            from sqlalchemy import select as _sel

            from shared.models import ArchitectAttempt
            attempt = (await session.execute(
                _sel(ArchitectAttempt).where(ArchitectAttempt.task_id == parent.id)
            )).scalar_one()
            if attempt.clarification_answer is not None:
                break
            await asyncio.sleep(0.05)

        # 3. RESOLVED handler runs — transition + resume.
        from run import on_architect_clarification_resolved
        await on_architect_clarification_resolved(Event(
            type=TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED,
            task_id=parent.id,
        ))
        # Give architect.resume a tick.
        for _ in range(10):
            await session.refresh(parent)
            if parent.trio_backlog is not None:
                break
            await asyncio.sleep(0.05)

    # Final state: TRIO_EXECUTING, backlog populated.
    await session.refresh(parent)
    assert parent.status == TaskStatus.TRIO_EXECUTING
    assert parent.trio_backlog is not None
    assert len(parent.trio_backlog) == 1
    assert parent.trio_backlog[0]["title"] == "Init app"
