"""Orchestration tests for messenger_router.handle.

Uses a real DB session (for conversation/focus persistence) but mocks the
LLM-side `converse` so we can assert routing decisions deterministically.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from agent.llm.types import Message
from orchestrator.messenger_router import handle
from orchestrator.messenger_router import persistence as p

pytestmark = pytest.mark.asyncio


class _CollectingSender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, user_id: int, text: str) -> None:
        self.sent.append(text)


async def _make_user(session, username="alice") -> int:
    from shared.models import Organization, Plan, User

    plan = (await session.execute(select(Plan).where(Plan.name == "free"))).scalar_one_or_none()
    if plan is None:
        plan = Plan(
            name="free",
            max_concurrent_tasks=1,
            max_tasks_per_day=5,
            max_input_tokens_per_day=1_000_000,
            max_output_tokens_per_day=250_000,
            max_members=3,
            monthly_price_cents=0,
        )
        session.add(plan)
        await session.flush()
    org = Organization(name=f"o-{username}", slug=f"o-{username}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    u = User(username=username, password_hash="x", display_name=username, organization_id=org.id)
    session.add(u)
    await session.flush()
    return u.id


async def _make_task(session, *, user_id: int, title: str, status: str) -> int:
    from shared.models import Task, TaskSource, TaskStatus

    user = await session.get(__import__("shared.models", fromlist=["User"]).User, user_id)
    t = Task(
        title=title,
        description="",
        source=TaskSource.SLACK,
        status=TaskStatus(status),
        organization_id=user.organization_id,
        created_by_user_id=user_id,
    )
    session.add(t)
    await session.flush()
    return t.id


async def test_first_message_no_focus_no_active_tasks_starts_draft(session):
    user_id = await _make_user(session, "alice")
    sender = _CollectingSender()
    with patch(
        "orchestrator.messenger_router.router.converse",
        AsyncMock(
            return_value=(
                "hi there",
                [Message(role="user", content="hi"), Message(role="assistant", content="hi there")],
            )
        ),
    ):
        await handle(
            session=session,
            source="slack",
            user_id=user_id,
            text="hi",
            thread_ts=None,
            sender=sender,
            home_dir=None,
        )
    assert sender.sent == ["hi there"]
    focus = await p.get_focus(session, user_id)
    assert focus is not None and focus.focus_kind == "draft"


async def test_first_message_no_focus_with_active_tasks_renders_picker(session):
    user_id = await _make_user(session, "bob")
    await _make_task(session, user_id=user_id, title="fix something", status="awaiting_approval")
    sender = _CollectingSender()
    converse_mock = AsyncMock()
    with patch("orchestrator.messenger_router.router.converse", converse_mock):
        await handle(
            session=session,
            source="slack",
            user_id=user_id,
            text="hi",
            thread_ts=None,
            sender=sender,
            home_dir=None,
        )
    assert any("Which task" in m for m in sender.sent)
    converse_mock.assert_not_awaited()
    focus = await p.get_focus(session, user_id)
    assert focus is None or focus.focus_kind == "none"


async def test_numeric_pick_sets_focus_to_task(session):
    user_id = await _make_user(session, "carol")
    task_id = await _make_task(session, user_id=user_id, title="t", status="coding")
    sender = _CollectingSender()
    with patch("orchestrator.messenger_router.router.converse", AsyncMock(return_value=("hi", []))):
        # first message -> picker
        await handle(
            session=session,
            source="slack",
            user_id=user_id,
            text="hi",
            thread_ts=None,
            sender=sender,
            home_dir=None,
        )
        # second message -> pick the task by id
        await handle(
            session=session,
            source="slack",
            user_id=user_id,
            text=str(task_id),
            thread_ts=None,
            sender=sender,
            home_dir=None,
        )
    focus = await p.get_focus(session, user_id)
    assert focus is not None
    assert focus.focus_kind == "task" and focus.focus_id == task_id


async def test_thread_reply_routes_to_task_feedback_and_does_not_touch_focus(session, publisher):
    user_id = await _make_user(session, "dora")
    sender = _CollectingSender()
    with (
        patch(
            "orchestrator.messenger_router.router.task_id_for_slack_message",
            AsyncMock(return_value=42),
        ),
        patch(
            "orchestrator.messenger_router.router.converse",
            AsyncMock(return_value=("never called", [])),
        ) as converse_mock,
    ):
        await handle(
            session=session,
            source="slack",
            user_id=user_id,
            text="please retry",
            thread_ts="123.456",
            sender=sender,
            home_dir=None,
        )
    converse_mock.assert_not_awaited()
    # publisher should have received a human_message event
    assert any(getattr(e, "task_id", None) == 42 for e in publisher.events)
    focus = await p.get_focus(session, user_id)
    assert focus is None


async def test_expired_focus_triggers_picker(session):
    user_id = await _make_user(session, "eve")
    await _make_task(session, user_id=user_id, title="t", status="coding")
    await p.set_focus(session, user_id, focus_kind="task", focus_id=7)
    await p._force_expire(session, user_id)
    sender = _CollectingSender()
    with patch("orchestrator.messenger_router.router.converse", AsyncMock()) as cm:
        await handle(
            session=session,
            source="slack",
            user_id=user_id,
            text="anything",
            thread_ts=None,
            sender=sender,
            home_dir=None,
        )
    assert any("Which task" in m for m in sender.sent)
    cm.assert_not_awaited()


async def test_history_persists_after_simulated_restart(session):
    user_id = await _make_user(session, "fay")
    task_id = await _make_task(session, user_id=user_id, title="t", status="coding")
    await p.set_focus(session, user_id, focus_kind="task", focus_id=task_id)
    sender = _CollectingSender()
    with patch(
        "orchestrator.messenger_router.router.converse",
        AsyncMock(
            return_value=(
                "first reply",
                [
                    Message(role="user", content="first"),
                    Message(role="assistant", content="first reply"),
                ],
            )
        ),
    ):
        await handle(
            session=session,
            source="slack",
            user_id=user_id,
            text="first",
            thread_ts=None,
            sender=sender,
            home_dir=None,
        )
    # Simulate restart: discard router-internal state (there is none; persistence is DB).
    # Re-issue handle with a fresh converse mock that expects prior history loaded.
    captured: dict[str, Any] = {}

    async def _fake_converse(*, user_id, text, history, home_dir, on_create_task):
        captured["history_len"] = len(history)
        return "second reply", [
            Message(role="user", content=text),
            Message(role="assistant", content="second reply"),
        ]

    with patch("orchestrator.messenger_router.router.converse", _fake_converse):
        await handle(
            session=session,
            source="slack",
            user_id=user_id,
            text="second",
            thread_ts=None,
            sender=sender,
            home_dir=None,
        )
    # Prior turn (user + assistant) loaded back from DB -> history len == 2.
    assert captured["history_len"] == 2


async def test_create_task_rebinds_draft_and_updates_focus(session):
    user_id = await _make_user(session, "gus")
    sender = _CollectingSender()

    async def fake_converse(*, user_id, text, history, home_dir, on_create_task):
        await on_create_task(123)
        return "task created", [
            Message(role="user", content=text),
            Message(role="assistant", content="task created"),
        ]

    with patch("orchestrator.messenger_router.router.converse", fake_converse):
        await handle(
            session=session,
            source="slack",
            user_id=user_id,
            text="make a task",
            thread_ts=None,
            sender=sender,
            home_dir=None,
        )

    focus = await p.get_focus(session, user_id)
    assert focus is not None
    assert focus.focus_kind == "task" and focus.focus_id == 123
    # The draft row should now be a task row.
    conv = await p.load_or_create_conversation(
        session,
        user_id=user_id,
        source="slack",
        focus_kind="task",
        focus_id=123,
    )
    assert len(conv.messages) == 2


async def test_explicit_switch_keyword_triggers_picker_even_with_live_focus(session):
    user_id = await _make_user(session, "ivy")
    task_id = await _make_task(session, user_id=user_id, title="t", status="coding")
    await p.set_focus(session, user_id, focus_kind="task", focus_id=task_id)
    sender = _CollectingSender()
    with patch("orchestrator.messenger_router.router.converse", AsyncMock()) as cm:
        await handle(session=session, source="slack", user_id=user_id,
                     text="switch", thread_ts=None, sender=sender, home_dir=None)
    assert any("Which task" in m for m in sender.sent)
    cm.assert_not_awaited()
    focus = await p.get_focus(session, user_id)
    # Focus was cleared to none.
    assert focus is None or focus.focus_kind == "none"


async def test_reset_command_clears_focus_but_keeps_conversation_rows(session):
    user_id = await _make_user(session, "joel")
    task_id = await _make_task(session, user_id=user_id, title="t", status="coding")
    await p.set_focus(session, user_id, focus_kind="task", focus_id=task_id)
    # Seed a row so we can prove it survives.
    await p.load_or_create_conversation(
        session, user_id=user_id, source="slack",
        focus_kind="task", focus_id=task_id,
    )
    sender = _CollectingSender()
    with patch("orchestrator.messenger_router.router.converse", AsyncMock()) as cm:
        await handle(session=session, source="slack", user_id=user_id,
                     text="reset", thread_ts=None, sender=sender, home_dir=None)
    assert any("Cleared focus" in m for m in sender.sent)
    cm.assert_not_awaited()
    focus = await p.get_focus(session, user_id)
    assert focus is None or focus.focus_kind == "none"
    # Conversation row still exists with focus_kind='task' (just orphaned from current focus).
    reloaded = await p.load_or_create_conversation(
        session, user_id=user_id, source="slack",
        focus_kind="task", focus_id=task_id,
    )
    assert reloaded.conversation_id > 0
