"""Loop guard — after TRIO_MAX_CLARIFICATIONS rounds, parent goes BLOCKED."""
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
async def test_fourth_clarification_blocks_parent(session, publisher):
    """3 prior clarification rounds + a 4th → parent transitions to BLOCKED."""
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
    slug = f"lg-{uuid.uuid4().hex[:8]}"
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
    # Seed three prior clarification rounds.
    for cycle in (1, 2, 3):
        session.add(ArchitectAttempt(
            task_id=parent.id, phase="INITIAL", cycle=cycle,
            reasoning=f"round {cycle}", tool_calls=[],
            clarification_question=f"Q{cycle}",
            clarification_answer=f"A{cycle}",
            clarification_source="po",
            session_blob_path=f"trio-{parent.id}.json",
        ))
    await session.commit()

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=MagicMock(
        output=(
            '```json\n{"decision": {"action": "awaiting_clarification", '
            '"question": "Q4"}}\n```'
        ),
        messages=[], api_messages=[],
    ))

    with patch("agent.lifecycle.trio.architect.create_architect_agent",
               return_value=fake_agent), \
         patch("agent.lifecycle.trio.architect._prepare_parent_workspace",
               AsyncMock(return_value=MagicMock(root="/tmp/ws"))), \
         patch("agent.lifecycle.trio.architect.home_dir_for_task",
               AsyncMock(return_value=None)), \
         patch("agent.session.Session.save", AsyncMock()), \
         patch.dict(os.environ, {"TRIO_MAX_CLARIFICATIONS": "3"}):
        from agent.lifecycle.trio import architect
        await architect.run_initial(parent.id)

    await session.refresh(parent)
    assert parent.status == TaskStatus.BLOCKED
