"""DB read/write helpers for the messenger router."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.messenger_router.types import (
    FOCUS_TTL_HOURS,
    MAX_HISTORY_MESSAGES,
    FocusKind,
    LoadedConversation,
)
from shared.models import MessengerConversation, UserFocus


async def load_or_create_conversation(
    session: AsyncSession,
    *,
    user_id: int,
    source: str,
    focus_kind: FocusKind,
    focus_id: int | None,
) -> LoadedConversation:
    stmt = select(MessengerConversation).where(
        MessengerConversation.user_id == user_id,
        MessengerConversation.source == source,
        MessengerConversation.focus_kind == focus_kind,
        MessengerConversation.focus_id.is_(focus_id) if focus_id is None
        else MessengerConversation.focus_id == focus_id,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = MessengerConversation(
            user_id=user_id, source=source,
            focus_kind=focus_kind, focus_id=focus_id,
            messages_json=[],
        )
        session.add(row)
        await session.flush()
    return LoadedConversation(
        conversation_id=row.id,
        user_id=row.user_id,
        source=row.source,
        focus_kind=row.focus_kind,           # type: ignore[arg-type]
        focus_id=row.focus_id,
        messages=list(row.messages_json or []),
        last_active_at=row.last_active_at,
    )


async def append_messages(
    session: AsyncSession,
    conversation_id: int,
    new_messages: list[dict[str, Any]],
) -> None:
    """Append messages to a conversation row, cap at MAX_HISTORY_MESSAGES, persist."""
    row = await session.get(MessengerConversation, conversation_id)
    if row is None:
        raise ValueError(f"conversation {conversation_id} not found")
    existing = list(row.messages_json or [])
    existing.extend(new_messages)
    if len(existing) > MAX_HISTORY_MESSAGES:
        existing = existing[-MAX_HISTORY_MESSAGES:]
    row.messages_json = existing
    row.last_active_at = datetime.now(UTC)
    await session.flush()


async def rebind_draft_to_task(
    session: AsyncSession,
    conversation_id: int,
    *,
    new_task_id: int,
) -> None:
    """Rebind a 'draft' conversation row to 'task:N' (after create_task fires)."""
    stmt = (
        update(MessengerConversation)
        .where(
            MessengerConversation.id == conversation_id,
            MessengerConversation.focus_kind == "draft",
        )
        .values(focus_kind="task", focus_id=new_task_id)
    )
    await session.execute(stmt)
    await session.flush()


async def get_focus(
    session: AsyncSession,
    user_id: int,
) -> UserFocus | None:
    """Returns the live focus row, or None if unset OR expired."""
    row = await session.get(UserFocus, user_id)
    if row is None:
        return None
    if row.expires_at <= datetime.now(UTC):
        return None
    return row


async def set_focus(
    session: AsyncSession,
    user_id: int,
    *,
    focus_kind: FocusKind,
    focus_id: int | None,
) -> None:
    """Upsert user_focus + bump expires_at to now+24h."""
    now = datetime.now(UTC)
    expires = now + timedelta(hours=FOCUS_TTL_HOURS)
    stmt = insert(UserFocus).values(
        user_id=user_id,
        focus_kind=focus_kind,
        focus_id=focus_id,
        set_at=now,
        expires_at=expires,
    ).on_conflict_do_update(
        index_elements=[UserFocus.user_id],
        set_={
            "focus_kind": focus_kind,
            "focus_id": focus_id,
            "set_at": now,
            "expires_at": expires,
        },
    )
    await session.execute(stmt)
    await session.flush()


async def clear_focus(session: AsyncSession, user_id: int) -> None:
    """Drop focus to 'none' (used by the `reset` command)."""
    await set_focus(session, user_id, focus_kind="none", focus_id=None)


async def _force_expire(session: AsyncSession, user_id: int) -> None:
    """Test-only: shove expires_at into the past."""
    stmt = (
        update(UserFocus)
        .where(UserFocus.user_id == user_id)
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await session.execute(stmt)
    await session.flush()
