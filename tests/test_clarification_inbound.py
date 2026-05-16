"""handle_clarification_inbound dispatches by trio_phase."""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from shared.events import TaskEventType


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_inbound_with_trio_phase_writes_answer_and_publishes(
    session, publisher,
):
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
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"ib-{suffix}", slug=f"ib-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(name=f"r-{suffix}", url="https://github.com/o/r.git",
                organization_id=org.id, default_branch="main")
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
    await session.flush()
    session.add(ArchitectAttempt(
        task_id=parent.id, phase="INITIAL", cycle=1,
        reasoning="r", tool_calls=[],
        clarification_question="Q?",
        session_blob_path=f"trio-{parent.id}.json",
    ))
    await session.commit()

    from agent.lifecycle.conversation import handle_clarification_inbound
    await handle_clarification_inbound(parent.id, "Go with React, simpler.")

    from sqlalchemy import select as _sel
    attempt = (await session.execute(
        _sel(ArchitectAttempt).where(ArchitectAttempt.task_id == parent.id)
    )).scalar_one()
    assert attempt.clarification_answer == "Go with React, simpler."
    assert attempt.clarification_source == "user"

    resolved = [e for e in publisher.events
                if e.type == TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED
                and e.task_id == parent.id]
    assert len(resolved) == 1


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_inbound_without_trio_phase_delegates_to_existing_handler(
    session,
):
    """Planner case: trio_phase IS NULL → delegate to handle_clarification_response."""
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
    org = Organization(name=f"ibn-{suffix}", slug=f"ibn-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(name=f"r-{suffix}", url="https://github.com/o/r.git",
                organization_id=org.id, default_branch="main")
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_CLARIFICATION,
        complexity=TaskComplexity.COMPLEX,
        repo_id=repo.id, organization_id=org.id,
    )
    session.add(parent)
    await session.commit()

    delegate = AsyncMock()
    with patch("agent.lifecycle.conversation.handle_clarification_response",
               delegate):
        from agent.lifecycle.conversation import handle_clarification_inbound
        await handle_clarification_inbound(parent.id, "Use Postgres.")

    delegate.assert_called_once_with(parent.id, "Use Postgres.")


@pytest.mark.asyncio
async def test_handle_feedback_event_forwards_content_to_inbound():
    """Slack/Telegram thread reply → POST /tasks/{id}/messages → task.feedback
    event → handle_feedback_event extracts content and calls
    handle_clarification_inbound, which (when status is AWAITING_CLARIFICATION)
    resumes the grill loop.
    """
    from types import SimpleNamespace

    from shared.events import Event, TaskEventType

    fake_task = SimpleNamespace(id=42, status="awaiting_clarification")
    inbound = AsyncMock()
    with (
        patch("agent.lifecycle.conversation.get_task", AsyncMock(return_value=fake_task)),
        patch("agent.lifecycle.conversation.handle_clarification_inbound", inbound),
    ):
        from agent.lifecycle.conversation import handle_feedback_event

        await handle_feedback_event(
            Event(
                type=TaskEventType.FEEDBACK,
                task_id=42,
                payload={"message_id": 7, "sender": "slack:alice", "content": "React, please"},
            )
        )

    inbound.assert_called_once_with(42, "React, please")


@pytest.mark.asyncio
async def test_handle_feedback_event_is_no_op_without_content():
    """Missing or empty content → drop. Defensive against legacy producers
    that haven't been updated to pass content yet."""
    from shared.events import Event, TaskEventType

    inbound = AsyncMock()
    with patch("agent.lifecycle.conversation.handle_clarification_inbound", inbound):
        from agent.lifecycle.conversation import handle_feedback_event

        await handle_feedback_event(
            Event(
                type=TaskEventType.FEEDBACK,
                task_id=42,
                payload={"message_id": 7, "sender": "slack:alice"},
            )
        )

    inbound.assert_not_called()


@pytest.mark.asyncio
async def test_handle_feedback_event_reemits_human_message_for_awaiting_review():
    """ADR-017 — Slack/Telegram thread reply on a trio task in AWAITING_REVIEW
    must re-emit as human.message so route_human_message dispatches it to
    the iteration handler. (For AWAITING_CLARIFICATION the existing path
    still calls handle_clarification_inbound.)"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from agent.lifecycle.conversation import handle_feedback_event
    from shared.events import Event, HumanEventType, TaskEventType

    fake_task = SimpleNamespace(
        id=5, status="awaiting_review", complexity="complex_large",
    )
    publish_mock = AsyncMock()
    with (
        patch("agent.lifecycle.conversation.get_task",
              AsyncMock(return_value=fake_task)),
        patch("agent.lifecycle.conversation.publish", publish_mock),
    ):
        await handle_feedback_event(Event(
            type=TaskEventType.FEEDBACK,
            task_id=5,
            payload={"message_id": 1, "sender": "slack:alan",
                     "content": "make it smaller"},
        ))

    publish_mock.assert_awaited_once()
    emitted = publish_mock.await_args.args[0]
    assert emitted.type == HumanEventType.MESSAGE
    assert emitted.task_id == 5
    assert emitted.payload["message"] == "make it smaller"
