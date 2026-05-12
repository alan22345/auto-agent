"""DB-layer tests for orchestrator.messenger_router.persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.messenger_router import persistence as p
from orchestrator.messenger_router.types import FOCUS_TTL_HOURS

pytestmark = pytest.mark.asyncio


async def _make_user(session, username: str = "alice") -> int:
    from sqlalchemy import select

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
    org = Organization(name=f"org-{username}", slug=f"org-{username}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    user = User(username=username, password_hash="x", display_name=username, organization_id=org.id)
    session.add(user)
    await session.flush()
    return user.id


async def test_load_or_create_creates_draft_row_when_missing(session):
    user_id = await _make_user(session, "alice")
    conv = await p.load_or_create_conversation(
        session,
        user_id=user_id,
        source="slack",
        focus_kind="draft",
        focus_id=None,
    )
    assert conv.conversation_id > 0
    assert conv.messages == []
    assert conv.focus_kind == "draft"


async def test_load_or_create_returns_existing_row(session):
    user_id = await _make_user(session, "bob")
    a = await p.load_or_create_conversation(
        session,
        user_id=user_id,
        source="slack",
        focus_kind="task",
        focus_id=42,
    )
    b = await p.load_or_create_conversation(
        session,
        user_id=user_id,
        source="slack",
        focus_kind="task",
        focus_id=42,
    )
    assert a.conversation_id == b.conversation_id


async def test_append_messages_caps_history_at_200(session):
    user_id = await _make_user(session, "carol")
    conv = await p.load_or_create_conversation(
        session,
        user_id=user_id,
        source="slack",
        focus_kind="task",
        focus_id=1,
    )
    msgs = [{"role": "user", "content": f"msg {i}"} for i in range(250)]
    await p.append_messages(session, conv.conversation_id, msgs)
    reloaded = await p.load_or_create_conversation(
        session,
        user_id=user_id,
        source="slack",
        focus_kind="task",
        focus_id=1,
    )
    assert len(reloaded.messages) == 200
    assert reloaded.messages[0]["content"] == "msg 50"
    assert reloaded.messages[-1]["content"] == "msg 249"


async def test_get_focus_returns_none_when_unset(session):
    user_id = await _make_user(session, "dora")
    focus = await p.get_focus(session, user_id)
    assert focus is None


async def test_set_focus_upserts_and_bumps_expires_at(session):
    user_id = await _make_user(session, "eve")
    await p.set_focus(session, user_id, focus_kind="task", focus_id=7)
    focus = await p.get_focus(session, user_id)
    assert focus is not None
    assert focus.focus_kind == "task" and focus.focus_id == 7
    expected = datetime.now(UTC) + timedelta(hours=FOCUS_TTL_HOURS)
    assert abs((focus.expires_at - expected).total_seconds()) < 60


async def test_get_focus_treats_expired_as_none(session):
    user_id = await _make_user(session, "fay")
    await p.set_focus(session, user_id, focus_kind="task", focus_id=9)
    await p._force_expire(session, user_id)
    focus = await p.get_focus(session, user_id)
    assert focus is None


async def test_rebind_draft_to_task(session):
    user_id = await _make_user(session, "gus")
    draft = await p.load_or_create_conversation(
        session,
        user_id=user_id,
        source="slack",
        focus_kind="draft",
        focus_id=None,
    )
    await p.rebind_draft_to_task(session, draft.conversation_id, new_task_id=99)
    reloaded = await p.load_or_create_conversation(
        session,
        user_id=user_id,
        source="slack",
        focus_kind="task",
        focus_id=99,
    )
    assert reloaded.conversation_id == draft.conversation_id


async def test_sources_are_isolated(session):
    user_id = await _make_user(session, "hal")
    a = await p.load_or_create_conversation(
        session,
        user_id=user_id,
        source="slack",
        focus_kind="task",
        focus_id=5,
    )
    b = await p.load_or_create_conversation(
        session,
        user_id=user_id,
        source="telegram",
        focus_kind="task",
        focus_id=5,
    )
    assert a.conversation_id != b.conversation_id
