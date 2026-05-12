"""Source-agnostic messenger router orchestration."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from sqlalchemy import select

from agent.llm.types import Message
from agent.slack_assistant import converse
from orchestrator.messenger_router import persistence as p
from orchestrator.messenger_router.picker import parse_pick, render_picker
from shared.events import human_message, publish
from shared.models import Task, TaskStatus
from shared.task_channel import task_id_for_slack_message

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from orchestrator.messenger_router.types import FocusKind

log = logging.getLogger(__name__)


Sender = Callable[[int, str], Awaitable[None]]
"""Async callable that delivers ``text`` to ``user_id`` on the originating channel."""


_TERMINAL_STATUSES = {TaskStatus.DONE, TaskStatus.FAILED}


async def _active_tasks_for_user(
    session: AsyncSession,
    user_id: int,
) -> list[dict]:
    stmt = (
        select(Task)
        .where(Task.created_by_user_id == user_id)
        .where(~Task.status.in_(_TERMINAL_STATUSES))
        .order_by(Task.id.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [{"id": t.id, "title": t.title, "status": t.status.value} for t in rows]


async def _render_and_send_picker(
    session: AsyncSession,
    user_id: int,
    sender: Sender,
) -> None:
    actives = await _active_tasks_for_user(session, user_id)
    await sender(user_id, render_picker(actives))


async def handle(
    *,
    session: AsyncSession,
    source: str,
    user_id: int,
    text: str,
    thread_ts: str | None,
    sender: Sender,
    home_dir: str | None,
) -> None:
    """Process one inbound messenger DM end-to-end."""

    # 1. Thread fast path: bypass everything.
    if thread_ts:
        task_id = await task_id_for_slack_message(thread_ts)
        if task_id is not None:
            await publish(
                human_message(
                    task_id=task_id,
                    message=text,
                    source=source,
                )
            )
            await sender(user_id, f"✉️ Sent to task #{task_id}.")
            return
        # Thread didn't map to a known task — fall through to normal flow.

    # 2. Load focus (auto-expires per persistence layer).
    focus_row = await p.get_focus(session, user_id)
    focus_kind: FocusKind = focus_row.focus_kind if focus_row else "none"  # type: ignore[assignment]
    focus_id = focus_row.focus_id if focus_row else None

    # 3. Try to resolve as a picker reply if focus is none.
    if focus_kind == "none":
        actives = await _active_tasks_for_user(session, user_id)
        pick = parse_pick(text, active_task_ids=[t["id"] for t in actives])
        if pick is not None:
            kind, fid = pick
            await p.set_focus(session, user_id, focus_kind=kind, focus_id=fid)
            await p.load_or_create_conversation(
                session,
                user_id=user_id,
                source=source,
                focus_kind=kind,
                focus_id=fid,
            )
            if kind == "task":
                await sender(user_id, f"Picked up task #{fid}. What would you like to do?")
            else:
                await sender(user_id, "OK, what should we build?")
            return
        # No pick: if active tasks exist, render picker.
        if actives:
            await _render_and_send_picker(session, user_id, sender)
            return
        # No active tasks — promote to draft and continue.
        focus_kind = "draft"
        focus_id = None
        await p.set_focus(session, user_id, focus_kind="draft", focus_id=None)

    # 4. Explicit switch keywords are honored regardless of focus.
    stripped = text.strip().lower()
    if stripped in {"switch", "switch task", "new task", "my tasks", "list tasks", "what was i"}:
        await p.set_focus(session, user_id, focus_kind="none", focus_id=None)
        await _render_and_send_picker(session, user_id, sender)
        return

    # 5. `reset`/`clear`: drop focus, keep history rows.
    if stripped in {"reset", "clear"}:
        await p.set_focus(session, user_id, focus_kind="none", focus_id=None)
        await sender(user_id, "Cleared focus. Reply to start fresh or pick a task.")
        return

    # 6. Load the conversation row and call the LLM primitive.
    conv = await p.load_or_create_conversation(
        session,
        user_id=user_id,
        source=source,
        focus_kind=focus_kind,
        focus_id=focus_id,
    )
    history_msgs = [Message(**m) for m in conv.messages]

    async def _on_create_task(new_task_id: int) -> None:
        if focus_kind == "draft":
            await p.rebind_draft_to_task(session, conv.conversation_id, new_task_id=new_task_id)
        await p.set_focus(session, user_id, focus_kind="task", focus_id=new_task_id)

    reply_text, appended = await converse(
        user_id=user_id,
        text=text,
        history=history_msgs,
        home_dir=home_dir,
        on_create_task=_on_create_task,
    )

    # 7. Persist appended messages and bump focus TTL.
    # Message is a dataclass — use dataclasses.asdict() for serialisation.
    await p.append_messages(
        session,
        conv.conversation_id,
        [dataclasses.asdict(m) for m in appended],
    )
    # Always bump the focus TTL after a turn. Read the (possibly rebound)
    # focus so the draft→task rebind from on_create_task is reflected.
    refreshed = await p.get_focus(session, user_id)
    final_kind = refreshed.focus_kind if refreshed is not None else focus_kind
    final_id = refreshed.focus_id if refreshed is not None else focus_id
    await p.set_focus(session, user_id, focus_kind=final_kind, focus_id=final_id)

    await sender(user_id, reply_text)
