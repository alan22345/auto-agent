# Messenger Conversation Continuity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the in-memory Slack DM session with a durable, source-agnostic conversation store + per-user focus pointer with 24h TTL and a deterministic task picker, so DM context survives restarts and follows users across messengers.

**Architecture:** A new `orchestrator/messenger_router/` package owns conversation persistence (`messenger_conversations`), focus state (`user_focus`), and the picker. The existing `agent/slack_assistant.converse` becomes a pure LLM tool-loop primitive. `integrations/slack/main.py` calls the router for top-level DMs; the threaded-reply fast path is preserved.

**Tech Stack:** Python async, SQLAlchemy async ORM, Alembic, FastAPI runtime (no new deps), pytest-asyncio for tests, real Postgres for DB-bound tests.

**Spec:** [`docs/superpowers/specs/2026-05-12-messenger-conversation-continuity-design.md`](../specs/2026-05-12-messenger-conversation-continuity-design.md)

**Deviations from spec:**
1. The picker is stateless. Instead of storing a "pending picker" record in Redis, we parse any inbound message that matches `^\s*#?(\d+)\s*$` as a task-id selection against the user's current active-task list; messages matching `^\s*new\s*$` start a draft. Removes the Redis dependency from the router and a moving part from tests. Functionally equivalent UX.
2. The `pg_advisory_xact_lock`-based per-user serialisation is **deferred**. Slack-bolt's socket-mode handler serialises events per workspace, so concurrent writes for the same user are not a realistic contention surface in v1. Re-introduce if a "lost update" is ever observed in logs.

---

## File Structure

**New files**
- `migrations/versions/030_messenger_conversations.py` — Alembic migration for the two new tables.
- `orchestrator/messenger_router/__init__.py` — package entry, re-exports `handle`.
- `orchestrator/messenger_router/types.py` — `FocusKind` constants, `LoadedConversation` dataclass.
- `orchestrator/messenger_router/persistence.py` — DB read/write helpers for `messenger_conversations` + `user_focus`.
- `orchestrator/messenger_router/picker.py` — picker rendering + reply parsing.
- `orchestrator/messenger_router/router.py` — `handle` orchestration.
- `tests/test_messenger_router_persistence.py` — DB-layer tests.
- `tests/test_messenger_router_picker.py` — picker reply-parsing tests.
- `tests/test_messenger_router_handle.py` — orchestration tests with mocked sender + LLM.

**Modified files**
- `shared/models.py` — add `MessengerConversation` and `UserFocus` ORM classes (insert before the final `Base.metadata` reference, near the existing task models).
- `agent/slack_assistant.py` — drop `_sessions`, `SESSION_TTL_SECONDS`, `MAX_HISTORY_MESSAGES`, `_get_or_create_session`, `reset_session`; change `converse` signature.
- `integrations/slack/main.py` — replace direct `converse(...)` call at `:430-435` with `messenger_router.handle(...)` for top-level DMs; thread fast path at `:413-425` stays intact.
- `tests/test_slack_assistant.py` — update for the new `converse` signature (if it exists; check during T5).

---

## Task 1: Alembic migration for `messenger_conversations` + `user_focus`

**Files:**
- Create: `migrations/versions/030_messenger_conversations.py`

- [ ] **Step 1: Write the migration**

```python
"""030 — messenger_conversations + user_focus

Source-agnostic durable conversation history for messenger DMs (Slack
today, Telegram next), plus a per-user focus pointer with 24h TTL.

Revision ID: 030
Revises: 029
Create Date: 2026-05-12
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "messenger_conversations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("focus_kind", sa.String(length=32), nullable=False),
        sa.Column("focus_id", sa.BigInteger(), nullable=True),
        sa.Column("messages_json", JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "user_id", "source", "focus_kind", "focus_id",
            name="uq_msgconv_user_source_focus",
        ),
    )
    op.create_index(
        "ix_msgconv_user_recent",
        "messenger_conversations",
        ["user_id", "last_active_at"],
        postgresql_using="btree",
    )

    op.create_table(
        "user_focus",
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id"),
            primary_key=True,
        ),
        sa.Column("focus_kind", sa.String(length=32), nullable=False),
        sa.Column("focus_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "set_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_focus")
    op.drop_index("ix_msgconv_user_recent", table_name="messenger_conversations")
    op.drop_table("messenger_conversations")
```

- [ ] **Step 2: Apply migration locally**

Run: `docker compose exec auto-agent alembic upgrade head`
Expected: `Running upgrade 029 -> 030, messenger_conversations + user_focus`. No errors.

- [ ] **Step 3: Verify downgrade is clean**

Run: `docker compose exec auto-agent alembic downgrade -1 && docker compose exec auto-agent alembic upgrade head`
Expected: down + up round-trip succeeds.

- [ ] **Step 4: Commit**

```bash
git add migrations/versions/030_messenger_conversations.py
git commit -m "feat(db): migration for messenger_conversations + user_focus"
```

---

## Task 2: ORM models in `shared/models.py`

**Files:**
- Modify: `shared/models.py` (append the two new classes near the other task-related models, around line 245 after `TaskOutcome`)

- [ ] **Step 1: Add `MessengerConversation` and `UserFocus`**

Append to `shared/models.py` (immediately after the `TaskOutcome` class, before `ScheduledTask`):

```python
class MessengerConversation(Base):
    """Durable per-(user, source, focus) chat history for messenger DMs."""
    __tablename__ = "messenger_conversations"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "source", "focus_kind", "focus_id",
            name="uq_msgconv_user_source_focus",
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    source = Column(String(32), nullable=False)            # 'slack' | 'telegram' | ...
    focus_kind = Column(String(32), nullable=False)        # 'draft' | 'task' (v1)
    focus_id = Column(BigInteger, nullable=True)           # NULL for 'draft'; task.id for 'task'
    messages_json = Column(JSONB, nullable=False, default=list)
    last_active_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class UserFocus(Base):
    """Per-user 'what am I working on right now' pointer with 24h TTL.

    Not keyed on source — switching focus on Slack also takes effect on
    Telegram (and any future messenger).
    """
    __tablename__ = "user_focus"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    focus_kind = Column(String(32), nullable=False)        # 'draft' | 'task' | 'none'
    focus_id = Column(BigInteger, nullable=True)
    set_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
```

- [ ] **Step 2: Verify imports compile**

Run: `.venv/bin/python3 -c "from shared.models import MessengerConversation, UserFocus; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Run ruff**

Run: `ruff check shared/models.py`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add shared/models.py
git commit -m "feat(models): MessengerConversation + UserFocus ORM"
```

---

## Task 3: Persistence helpers (DB read/write layer)

**Files:**
- Create: `orchestrator/messenger_router/__init__.py`
- Create: `orchestrator/messenger_router/types.py`
- Create: `orchestrator/messenger_router/persistence.py`
- Create: `tests/test_messenger_router_persistence.py`

- [ ] **Step 1: Write `types.py`**

```python
"""Internal types for the messenger router."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

FocusKind = Literal["draft", "task", "none"]
"""v1 focus kinds. v2 will add 'freeform' and 'po_analysis'."""

# 24h focus TTL, per design.
FOCUS_TTL_HOURS = 24

# Cap conversation history rows at the most recent N messages.
MAX_HISTORY_MESSAGES = 200


@dataclass
class LoadedConversation:
    """In-memory view of a single conversation row."""

    conversation_id: int
    user_id: int
    source: str
    focus_kind: FocusKind
    focus_id: int | None
    messages: list[dict[str, Any]]   # raw message dicts as stored in jsonb
    last_active_at: datetime
```

- [ ] **Step 2: Write `__init__.py` (placeholder, expanded in later tasks)**

```python
"""Source-agnostic messenger router: durable per-user conversation
history + focus pointer for Slack, Telegram, and future channels.

Exports ``handle`` (added in router.py, re-exported in T6).
"""
```

- [ ] **Step 3: Write the failing persistence tests**

Create `tests/test_messenger_router_persistence.py`:

```python
"""DB-layer tests for orchestrator.messenger_router.persistence."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.messenger_router import persistence as p
from orchestrator.messenger_router.types import FOCUS_TTL_HOURS


pytestmark = pytest.mark.asyncio


async def _make_user(session, username: str = "alice") -> int:
    from shared.models import Organization, User
    org = Organization(name=f"org-{username}", slug=f"org-{username}")
    session.add(org)
    await session.flush()
    user = User(username=username, display_name=username, organization_id=org.id)
    session.add(user)
    await session.flush()
    return user.id


async def test_load_or_create_creates_draft_row_when_missing(session):
    user_id = await _make_user(session, "alice")
    conv = await p.load_or_create_conversation(
        session, user_id=user_id, source="slack",
        focus_kind="draft", focus_id=None,
    )
    assert conv.conversation_id > 0
    assert conv.messages == []
    assert conv.focus_kind == "draft"


async def test_load_or_create_returns_existing_row(session):
    user_id = await _make_user(session, "bob")
    a = await p.load_or_create_conversation(
        session, user_id=user_id, source="slack",
        focus_kind="task", focus_id=42,
    )
    b = await p.load_or_create_conversation(
        session, user_id=user_id, source="slack",
        focus_kind="task", focus_id=42,
    )
    assert a.conversation_id == b.conversation_id


async def test_append_messages_caps_history_at_200(session):
    user_id = await _make_user(session, "carol")
    conv = await p.load_or_create_conversation(
        session, user_id=user_id, source="slack",
        focus_kind="task", focus_id=1,
    )
    msgs = [{"role": "user", "content": f"msg {i}"} for i in range(250)]
    await p.append_messages(session, conv.conversation_id, msgs)
    reloaded = await p.load_or_create_conversation(
        session, user_id=user_id, source="slack",
        focus_kind="task", focus_id=1,
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
        session, user_id=user_id, source="slack",
        focus_kind="draft", focus_id=None,
    )
    await p.rebind_draft_to_task(session, draft.conversation_id, new_task_id=99)
    reloaded = await p.load_or_create_conversation(
        session, user_id=user_id, source="slack",
        focus_kind="task", focus_id=99,
    )
    assert reloaded.conversation_id == draft.conversation_id


async def test_sources_are_isolated(session):
    user_id = await _make_user(session, "hal")
    a = await p.load_or_create_conversation(
        session, user_id=user_id, source="slack",
        focus_kind="task", focus_id=5,
    )
    b = await p.load_or_create_conversation(
        session, user_id=user_id, source="telegram",
        focus_kind="task", focus_id=5,
    )
    assert a.conversation_id != b.conversation_id
```

- [ ] **Step 4: Run tests, confirm they fail**

Run: `DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/autoagent .venv/bin/python3 -m pytest tests/test_messenger_router_persistence.py -v`
Expected: ImportError or ModuleNotFoundError for `orchestrator.messenger_router.persistence`.

- [ ] **Step 5: Implement `persistence.py`**

Create `orchestrator/messenger_router/persistence.py`:

```python
"""DB read/write helpers for the messenger router."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import MessengerConversation, UserFocus
from orchestrator.messenger_router.types import (
    FOCUS_TTL_HOURS,
    MAX_HISTORY_MESSAGES,
    FocusKind,
    LoadedConversation,
)


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
```

- [ ] **Step 6: Run tests, confirm they pass**

Run: `DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/autoagent .venv/bin/python3 -m pytest tests/test_messenger_router_persistence.py -v`
Expected: all 8 tests pass.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/messenger_router/ tests/test_messenger_router_persistence.py
git commit -m "feat(messenger): persistence layer for conversations + focus"
```

---

## Task 4: Picker module (rendering + reply parsing)

**Files:**
- Create: `orchestrator/messenger_router/picker.py`
- Create: `tests/test_messenger_router_picker.py`

- [ ] **Step 1: Write the failing picker tests**

Create `tests/test_messenger_router_picker.py`:

```python
"""Picker reply-parsing tests. Pure function tests, no DB."""
from __future__ import annotations

from orchestrator.messenger_router.picker import parse_pick, render_picker


def test_parse_pick_numeric_id():
    assert parse_pick("42", active_task_ids=[42, 57]) == ("task", 42)


def test_parse_pick_hash_prefix():
    assert parse_pick("#42", active_task_ids=[42, 57]) == ("task", 42)


def test_parse_pick_with_whitespace():
    assert parse_pick("  42  ", active_task_ids=[42]) == ("task", 42)


def test_parse_pick_rejects_unknown_task_id():
    # 99 isn't in the active list — not a pick.
    assert parse_pick("99", active_task_ids=[42, 57]) is None


def test_parse_pick_new_starts_draft():
    assert parse_pick("new", active_task_ids=[1, 2]) == ("draft", None)
    assert parse_pick("NEW", active_task_ids=[1, 2]) == ("draft", None)
    assert parse_pick("  new ", active_task_ids=[1, 2]) == ("draft", None)


def test_parse_pick_returns_none_for_prose():
    assert parse_pick("hey what about task 42", active_task_ids=[42]) is None
    assert parse_pick("create a task", active_task_ids=[]) is None


def test_render_picker_includes_active_tasks_and_ids():
    text = render_picker(
        [
            {"id": 42, "title": "fix freeform PR rebase", "status": "awaiting_approval"},
            {"id": 57, "title": "add /test placeholder route", "status": "in_progress"},
        ]
    )
    assert "#42" in text
    assert "#57" in text
    assert "freeform PR rebase" in text
    assert "new" in text.lower()


def test_render_picker_with_no_active_tasks_offers_new_only():
    text = render_picker([])
    assert "new" in text.lower()
```

- [ ] **Step 2: Run, confirm failing**

Run: `.venv/bin/python3 -m pytest tests/test_messenger_router_picker.py -v`
Expected: ImportError for `orchestrator.messenger_router.picker`.

- [ ] **Step 3: Implement `picker.py`**

Create `orchestrator/messenger_router/picker.py`:

```python
"""Picker rendering + stateless pick-resolution.

The picker is stateless: when a user replies with a number or 'new', we
parse it against the user's current active-task list. No transient store.
"""
from __future__ import annotations

import re
from typing import Any

from orchestrator.messenger_router.types import FocusKind

_PICK_RE = re.compile(r"^\s*#?(\d+)\s*$")
_NEW_RE = re.compile(r"^\s*new\s*$", re.IGNORECASE)


def parse_pick(
    text: str,
    *,
    active_task_ids: list[int],
) -> tuple[FocusKind, int | None] | None:
    """Try to interpret ``text`` as a picker reply.

    Returns ``('task', id)`` for a numeric pick whose id is in
    ``active_task_ids``; ``('draft', None)`` for a 'new' reply; or
    ``None`` if the text doesn't look like a pick.
    """
    if _NEW_RE.match(text):
        return ("draft", None)
    m = _PICK_RE.match(text)
    if m:
        task_id = int(m.group(1))
        if task_id in active_task_ids:
            return ("task", task_id)
    return None


def render_picker(active_tasks: list[dict[str, Any]]) -> str:
    """Render the picker message body. Active tasks come pre-sorted."""
    if not active_tasks:
        return (
            "You don't have any active tasks. "
            "Reply `new` to start a fresh request."
        )
    lines = ["Which task do you want to pick up?"]
    for i, t in enumerate(active_tasks, start=1):
        title = (t.get("title") or "")[:80]
        status = t.get("status", "")
        lines.append(f"{i}. #{t['id']}  {title}  ({status})")
    lines.append("Reply with the task number (e.g. 42) or `new` to start fresh.")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `.venv/bin/python3 -m pytest tests/test_messenger_router_picker.py -v`
Expected: all 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/messenger_router/picker.py tests/test_messenger_router_picker.py
git commit -m "feat(messenger): picker rendering + stateless reply parsing"
```

---

## Task 5: Update `agent.slack_assistant.converse` signature + on_create_task callback

**Files:**
- Modify: `agent/slack_assistant.py` (rewrite `converse`, drop in-memory session state, accept history + callback)
- Modify: `tests/test_slack_assistant.py` if it exists (check first)

- [ ] **Step 1: Check for existing converse tests**

Run: `ls tests/ | grep slack_assistant`
Note any existing test file. If present, read it to understand current assertions.

- [ ] **Step 2: Write a failing test for the new signature**

Append to (or create) `tests/test_slack_assistant.py`:

```python
"""Tests for the LLM tool-loop primitive `converse`."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agent.llm.types import LLMResponse, Message, ToolCall
from agent.slack_assistant import converse


pytestmark = pytest.mark.asyncio


def _resp(content: str = "", tool_calls: list[ToolCall] | None = None,
          stop_reason: str = "end_turn") -> LLMResponse:
    return LLMResponse(
        message=Message(role="assistant", content=content, tool_calls=tool_calls or []),
        stop_reason=stop_reason,
        usage=None,
    )


async def test_converse_returns_appended_messages_and_reply():
    history: list[Message] = []
    fake_provider = AsyncMock()
    fake_provider.complete.return_value = _resp(content="hello!")
    with patch("agent.slack_assistant.get_provider", return_value=fake_provider), \
         patch("agent.slack_assistant.resolve_home_dir", return_value=None):
        reply, appended = await converse(
            user_id=1, text="hi", history=history,
            home_dir=None, on_create_task=None,
        )
    assert reply == "hello!"
    # appended = the user msg + the assistant reply (2 entries)
    assert len(appended) == 2
    assert appended[0].role == "user"
    assert appended[1].role == "assistant"


async def test_converse_invokes_on_create_task_when_create_task_tool_fires():
    history: list[Message] = []
    fake_provider = AsyncMock()
    fake_provider.complete.side_effect = [
        _resp(
            tool_calls=[ToolCall(id="t1", name="create_task", arguments={
                "repo_name": "cardamon", "description": "test task",
            })],
            stop_reason="tool_use",
        ),
        _resp(content="created!"),
    ]
    on_create_task = AsyncMock()
    with patch("agent.slack_assistant.get_provider", return_value=fake_provider), \
         patch("agent.slack_assistant.resolve_home_dir", return_value=None), \
         patch("agent.slack_assistant._create_task",
               AsyncMock(return_value={"task_id": 77, "status": "queued", "title": "x"})):
        reply, _ = await converse(
            user_id=1, text="create a test task on cardamon",
            history=history, home_dir=None, on_create_task=on_create_task,
        )
    assert reply == "created!"
    on_create_task.assert_awaited_once_with(77)
```

- [ ] **Step 3: Run the test, confirm failing**

Run: `.venv/bin/python3 -m pytest tests/test_slack_assistant.py -v`
Expected: TypeError or AttributeError — converse doesn't accept the new kwargs yet.

- [ ] **Step 4: Rewrite `agent/slack_assistant.py`**

Replace the entire file (preserving the SYSTEM_PROMPT, `_TOOL_DEFS`, and `_dispatch_tool` family) with:

```python
"""Slack-DM conversational primitive (LLM tool loop).

Wraps a Claude tool-using loop with the auto-agent task-management tools.
The caller (orchestrator.messenger_router) owns conversation history
persistence — this module is a pure compute step.
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

import httpx

from agent.llm import get_provider
from agent.llm.types import Message, ToolCall, ToolDefinition
from orchestrator.claude_auth import resolve_home_dir
from shared.config import settings
from shared.events import human_message, publish

log = logging.getLogger(__name__)

ORCHESTRATOR_URL = settings.orchestrator_url
MAX_TURNS_PER_REQUEST = 8


SYSTEM_PROMPT = """\
You are auto-agent. You manage software-engineering tasks on behalf of a
small team via Slack DMs. You have tools that call the auto-agent API; \
the actual implementation work happens in a separate coding pipeline once \
a task is queued.

How to behave:
- Always check details with the user before doing anything that creates, \
  cancels, approves, or rejects work. Don't guess. If the user is vague \
  about which repo, which task, or what they want done, ask one focused \
  follow-up question.
- After running a tool, summarise the result in one or two sentences. \
  Don't paste raw JSON.
- Plain prose, friendly, brief. Skip emojis unless the user uses them.
- One question at a time when clarifying.

You can:
- List the user's tasks (filter by status if helpful).
- Read a specific task's status, plan, or PR.
- Create a new task on a named repo (after confirming the repo + the \
  description).
- Approve or reject a plan that's awaiting approval.
- Send a clarification answer to a task that's asked one.
- Cancel a running task.
- List the available repos so you can match a name the user gave you \
  (e.g. "the cardamon repo" → look it up before creating).

What you don't do: write code, run commands, or do the engineering work \
yourself. Your job is to talk to the user, gather what's needed, and call \
the right tool.\
"""


# --- Tool definitions (unchanged from previous version) ---------------------

_TOOL_DEFS: list[ToolDefinition] = [
    # ... (verbatim from previous file lines 81-204; copy as-is)
]

# --- Tool dispatchers (unchanged from previous version) ---------------------
# Keep _list_my_tasks, _get_task, _list_repos, _create_task, _approve_plan,
# _reject_plan, _answer_clarification, _cancel_task verbatim.
# (Copy lines 234-353 from the previous file.)
# ----------------------------------------------------------------------------


async def _dispatch_tool(name: str, args: dict, user_id: int) -> tuple[object, int | None]:
    """Run a tool. Returns (result, created_task_id_if_any)."""
    created_task_id: int | None = None
    try:
        if name == "list_my_tasks":
            result = await _list_my_tasks(user_id, args.get("status", "active"))
        elif name == "get_task":
            result = await _get_task(int(args["task_id"]))
        elif name == "list_repos":
            result = await _list_repos()
        elif name == "create_task":
            result = await _create_task(
                user_id, args["repo_name"], args["description"], args.get("title"),
            )
            if isinstance(result, dict) and "task_id" in result:
                created_task_id = int(result["task_id"])
        elif name == "approve_plan":
            result = await _approve_plan(int(args["task_id"]), args.get("feedback", ""))
        elif name == "reject_plan":
            result = await _reject_plan(int(args["task_id"]), args["feedback"])
        elif name == "answer_clarification":
            result = await _answer_clarification(int(args["task_id"]), args["answer"])
        elif name == "cancel_task":
            result = await _cancel_task(int(args["task_id"]))
        else:
            result = {"error": f"unknown tool: {name}"}
    except KeyError as e:
        result = {"error": f"missing required argument: {e}"}
    except Exception as e:
        log.exception("tool dispatch failed")
        result = {"error": f"tool error: {e}"}
    return result, created_task_id


async def converse(
    *,
    user_id: int,
    text: str,
    history: list[Message],
    home_dir: str | None,
    on_create_task: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, list[Message]]:
    """Run one DM turn through the LLM tool loop.

    Args:
        user_id:      auto-agent users.id
        text:         the new user message
        history:      prior conversation (list of Message); a copy is used internally
        home_dir:     the user's resolved Claude vault, or None for fallback
        on_create_task: optional async callback invoked with the new task id
                        immediately after a successful ``create_task`` tool call

    Returns:
        (reply_text, appended_messages) where ``appended_messages`` is the
        sequence of new turns to be persisted by the caller (user msg + any
        assistant/tool turns).
    """
    appended: list[Message] = [Message(role="user", content=text)]
    working = list(history) + list(appended)

    provider = get_provider(model_override="fast", home_dir=home_dir)

    final_text = ""
    for _turn in range(MAX_TURNS_PER_REQUEST):
        try:
            response = await provider.complete(
                messages=working, system=SYSTEM_PROMPT,
                tools=_TOOL_DEFS, max_tokens=2048,
            )
        except Exception as e:
            log.exception("slack assistant LLM call failed")
            return f"(internal error: {e})", appended

        working.append(response.message)
        appended.append(response.message)

        if response.stop_reason != "tool_use" or not response.message.tool_calls:
            final_text = response.message.content or ""
            break

        for call in response.message.tool_calls:
            result, created_task_id = await _dispatch_tool(call.name, call.arguments, user_id)
            tool_msg = Message(
                role="tool",
                content=json.dumps(result, default=str)[:8000],
                tool_call_id=call.id,
                tool_name=call.name,
            )
            working.append(tool_msg)
            appended.append(tool_msg)
            if created_task_id is not None and on_create_task is not None:
                await on_create_task(created_task_id)

    if not final_text:
        final_text = (
            "I got stuck thinking about that — try rephrasing or say "
            "`reset` to start over."
        )
    return final_text, appended
```

When copying `_TOOL_DEFS` and the tool dispatchers (`_list_my_tasks` etc.), pull them verbatim from the previous version of `agent/slack_assistant.py` (lines 81–353 in the pre-change file). Do NOT modify them.

Delete: `_sessions`, `SESSION_TTL_SECONDS`, `MAX_HISTORY_MESSAGES`, `_get_or_create_session`, `reset_session`.

- [ ] **Step 5: Run tests, confirm pass**

Run: `.venv/bin/python3 -m pytest tests/test_slack_assistant.py -v`
Expected: both tests pass.

- [ ] **Step 6: Run ruff**

Run: `ruff check agent/slack_assistant.py`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add agent/slack_assistant.py tests/test_slack_assistant.py
git commit -m "refactor(slack-assistant): pure LLM primitive + on_create_task callback"
```

---

## Task 6: Router orchestration (`router.py` + `__init__.py` export + handle tests)

**Files:**
- Create: `orchestrator/messenger_router/router.py`
- Modify: `orchestrator/messenger_router/__init__.py` (re-export `handle`)
- Create: `tests/test_messenger_router_handle.py`

- [ ] **Step 1: Write the failing orchestration tests**

Create `tests/test_messenger_router_handle.py`:

```python
"""Orchestration tests for messenger_router.handle.

Uses a real DB session (for conversation/focus persistence) but mocks the
LLM-side `converse` so we can assert routing decisions deterministically.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.messenger_router import handle, persistence as p
from agent.llm.types import Message


pytestmark = pytest.mark.asyncio


class _CollectingSender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, user_id: int, text: str) -> None:
        self.sent.append(text)


async def _make_user(session, username="alice") -> int:
    from shared.models import Organization, User
    org = Organization(name=f"o-{username}", slug=f"o-{username}")
    session.add(org); await session.flush()
    u = User(username=username, display_name=username, organization_id=org.id)
    session.add(u); await session.flush()
    return u.id


async def _make_task(session, *, user_id: int, title: str, status: str) -> int:
    from shared.models import Task, TaskSource, TaskStatus
    user = await session.get(__import__("shared.models", fromlist=["User"]).User, user_id)
    t = Task(
        title=title, description="", source=TaskSource.SLACK,
        status=TaskStatus(status), organization_id=user.organization_id,
        created_by_user_id=user_id,
    )
    session.add(t); await session.flush()
    return t.id


async def test_first_message_no_focus_no_active_tasks_starts_draft(session):
    user_id = await _make_user(session, "alice")
    sender = _CollectingSender()
    with patch("orchestrator.messenger_router.router.converse",
               AsyncMock(return_value=("hi there", [Message(role="user", content="hi"),
                                                     Message(role="assistant", content="hi there")]))):
        await handle(session=session, source="slack", user_id=user_id,
                     text="hi", thread_ts=None, sender=sender,
                     home_dir=None)
    assert sender.sent == ["hi there"]
    focus = await p.get_focus(session, user_id)
    assert focus is not None and focus.focus_kind == "draft"


async def test_first_message_no_focus_with_active_tasks_renders_picker(session):
    user_id = await _make_user(session, "bob")
    await _make_task(session, user_id=user_id, title="fix something", status="awaiting_approval")
    sender = _CollectingSender()
    converse_mock = AsyncMock()
    with patch("orchestrator.messenger_router.router.converse", converse_mock):
        await handle(session=session, source="slack", user_id=user_id,
                     text="hi", thread_ts=None, sender=sender, home_dir=None)
    assert any("Which task" in m for m in sender.sent)
    converse_mock.assert_not_awaited()
    focus = await p.get_focus(session, user_id)
    assert focus is None or focus.focus_kind == "none"


async def test_numeric_pick_sets_focus_to_task(session):
    user_id = await _make_user(session, "carol")
    task_id = await _make_task(session, user_id=user_id, title="t", status="coding")
    sender = _CollectingSender()
    with patch("orchestrator.messenger_router.router.converse",
               AsyncMock(return_value=("hi", []))):
        # first message -> picker
        await handle(session=session, source="slack", user_id=user_id,
                     text="hi", thread_ts=None, sender=sender, home_dir=None)
        # second message -> pick the task by id
        await handle(session=session, source="slack", user_id=user_id,
                     text=str(task_id), thread_ts=None, sender=sender, home_dir=None)
    focus = await p.get_focus(session, user_id)
    assert focus is not None
    assert focus.focus_kind == "task" and focus.focus_id == task_id


async def test_thread_reply_routes_to_task_feedback_and_does_not_touch_focus(session, publisher):
    user_id = await _make_user(session, "dora")
    sender = _CollectingSender()
    with patch("orchestrator.messenger_router.router.task_id_for_slack_message",
               AsyncMock(return_value=42)), \
         patch("orchestrator.messenger_router.router.converse",
               AsyncMock(return_value=("never called", []))) as converse_mock:
        await handle(session=session, source="slack", user_id=user_id,
                     text="please retry", thread_ts="123.456",
                     sender=sender, home_dir=None)
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
        await handle(session=session, source="slack", user_id=user_id,
                     text="anything", thread_ts=None, sender=sender, home_dir=None)
    assert any("Which task" in m for m in sender.sent)
    cm.assert_not_awaited()


async def test_history_persists_after_simulated_restart(session):
    user_id = await _make_user(session, "fay")
    task_id = await _make_task(session, user_id=user_id, title="t", status="coding")
    await p.set_focus(session, user_id, focus_kind="task", focus_id=task_id)
    sender = _CollectingSender()
    with patch("orchestrator.messenger_router.router.converse",
               AsyncMock(return_value=("first reply",
                                       [Message(role="user", content="first"),
                                        Message(role="assistant", content="first reply")]))):
        await handle(session=session, source="slack", user_id=user_id,
                     text="first", thread_ts=None, sender=sender, home_dir=None)
    # Simulate restart: discard router-internal state (there is none; persistence is DB).
    # Re-issue handle with a fresh converse mock that expects prior history loaded.
    captured: dict[str, Any] = {}
    async def _fake_converse(*, user_id, text, history, home_dir, on_create_task):
        captured["history_len"] = len(history)
        return "second reply", [Message(role="user", content=text),
                                 Message(role="assistant", content="second reply")]
    with patch("orchestrator.messenger_router.router.converse", _fake_converse):
        await handle(session=session, source="slack", user_id=user_id,
                     text="second", thread_ts=None, sender=sender, home_dir=None)
    # Prior turn (user + assistant) loaded back from DB → history len == 2.
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
        await handle(session=session, source="slack", user_id=user_id,
                     text="make a task", thread_ts=None, sender=sender, home_dir=None)

    focus = await p.get_focus(session, user_id)
    assert focus is not None
    assert focus.focus_kind == "task" and focus.focus_id == 123
    # The draft row should now be a task row.
    conv = await p.load_or_create_conversation(
        session, user_id=user_id, source="slack", focus_kind="task", focus_id=123,
    )
    assert len(conv.messages) == 2
```

- [ ] **Step 2: Run tests, confirm failing**

Run: `DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/autoagent .venv/bin/python3 -m pytest tests/test_messenger_router_handle.py -v`
Expected: ImportError (no `handle` exported yet).

- [ ] **Step 3: Implement `router.py`**

Create `orchestrator/messenger_router/router.py`:

```python
"""Source-agnostic messenger router orchestration."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.llm.types import Message
from agent.slack_assistant import converse
from orchestrator.messenger_router import persistence as p
from orchestrator.messenger_router.picker import parse_pick, render_picker
from orchestrator.messenger_router.types import FocusKind
from shared.events import human_message, publish
from shared.models import Task, TaskStatus
from shared.task_channel import task_id_for_slack_message

log = logging.getLogger(__name__)


Sender = Callable[[int, str], Awaitable[None]]
"""Async callable that delivers ``text`` to ``user_id`` on the originating channel."""


_TERMINAL_STATUSES = {TaskStatus.DONE, TaskStatus.FAILED}


async def _active_tasks_for_user(
    session: AsyncSession, user_id: int,
) -> list[dict]:
    stmt = (
        select(Task)
        .where(Task.created_by_user_id == user_id)
        .where(~Task.status.in_(_TERMINAL_STATUSES))
        .order_by(Task.id.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {"id": t.id, "title": t.title, "status": t.status.value}
        for t in rows
    ]


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
    thread_ts: Optional[str],
    sender: Sender,
    home_dir: Optional[str],
) -> None:
    """Process one inbound messenger DM end-to-end."""

    # 1. Thread fast path: bypass everything.
    if thread_ts:
        task_id = await task_id_for_slack_message(thread_ts)
        if task_id is not None:
            await publish(human_message(
                task_id=task_id, message=text, source=source,
            ))
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
                session, user_id=user_id, source=source,
                focus_kind=kind, focus_id=fid,
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
    if stripped in {"switch", "switch task", "new task", "my tasks",
                    "list tasks", "what was i"}:
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
        session, user_id=user_id, source=source,
        focus_kind=focus_kind, focus_id=focus_id,
    )
    history_msgs = [Message(**m) for m in conv.messages]

    async def _on_create_task(new_task_id: int) -> None:
        if focus_kind == "draft":
            await p.rebind_draft_to_task(session, conv.conversation_id, new_task_id=new_task_id)
        await p.set_focus(session, user_id, focus_kind="task", focus_id=new_task_id)

    reply_text, appended = await converse(
        user_id=user_id, text=text, history=history_msgs,
        home_dir=home_dir, on_create_task=_on_create_task,
    )

    # 7. Persist appended messages and bump focus TTL.
    await p.append_messages(
        session, conv.conversation_id,
        [m.model_dump() if hasattr(m, "model_dump") else dict(m) for m in appended],
    )
    # Reload focus_kind/focus_id in case on_create_task rebinded the draft.
    refreshed = await p.get_focus(session, user_id)
    if refreshed is not None:
        await p.set_focus(
            session, user_id,
            focus_kind=refreshed.focus_kind, focus_id=refreshed.focus_id,
        )

    await sender(user_id, reply_text)
```

- [ ] **Step 4: Wire `handle` export**

Replace `orchestrator/messenger_router/__init__.py` with:

```python
"""Source-agnostic messenger router."""
from orchestrator.messenger_router.router import handle

__all__ = ["handle"]
```

- [ ] **Step 5: Run tests, confirm pass**

Run: `DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/autoagent .venv/bin/python3 -m pytest tests/test_messenger_router_handle.py -v`
Expected: all 7 tests pass.

- [ ] **Step 6: Run full router test suite**

Run: `DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/autoagent .venv/bin/python3 -m pytest tests/test_messenger_router_persistence.py tests/test_messenger_router_picker.py tests/test_messenger_router_handle.py -v`
Expected: all 23 tests pass.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/messenger_router/router.py orchestrator/messenger_router/__init__.py tests/test_messenger_router_handle.py
git commit -m "feat(messenger): router.handle orchestration + draft→task rebind"
```

---

## Task 7: Wire `integrations/slack/main.py` to the router

**Files:**
- Modify: `integrations/slack/main.py` (replace the `converse` call site at `:427-440`)

- [ ] **Step 1: Read the current handler**

Read: `integrations/slack/main.py:340-441` to confirm the current dispatch shape.

- [ ] **Step 2: Replace the conversational assistant block**

Find this block at `integrations/slack/main.py:427-440`:

```python
    # Everything else flows through the conversational assistant — it
    # decides whether to chat back, ask a clarifying question, or call a
    # tool to act on the user's behalf.
    from agent.slack_assistant import converse

    try:
        reply = await converse(
            slack_user_id=slack_user_id, user_id=user["id"], text=text, org_id=org_id
        )
    except Exception as e:
        log.exception("slack assistant crashed")
        reply = f"(internal error: {e})"
    if reply:
        await send_slack_dm(slack_user_id, reply, org_id=org_id)
```

Replace it with:

```python
    # Everything else flows through the source-agnostic messenger router,
    # which owns durable conversation state + focus.
    from orchestrator.claude_auth import resolve_home_dir
    from orchestrator.messenger_router import handle as router_handle
    from shared.database import async_session

    async def _sender(target_user_id: int, body: str) -> None:
        await send_slack_dm(slack_user_id, body, org_id=org_id)

    home_dir = await resolve_home_dir(user["id"])

    try:
        async with async_session() as db:
            await router_handle(
                session=db, source="slack",
                user_id=user["id"], text=text, thread_ts=None,
                sender=_sender, home_dir=home_dir,
            )
            await db.commit()
    except Exception:
        log.exception("messenger_router crashed")
        await send_slack_dm(slack_user_id, "(internal error)", org_id=org_id)
```

Note: we already handled `thread_ts` in the existing block at `:413-425`, so we always pass `thread_ts=None` here. The router's thread-fast-path is defense in depth for future callers but won't fire from this call site.

- [ ] **Step 3: Run ruff**

Run: `ruff check integrations/slack/main.py`
Expected: no errors.

- [ ] **Step 4: Run the full unit suite**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: previous passes hold; any test that exercised the old `converse` signature was already updated in T5.

- [ ] **Step 5: Commit**

```bash
git add integrations/slack/main.py
git commit -m "feat(slack): route DMs through messenger_router"
```

---

## Task 8: Verification, lint, manual smoke

- [ ] **Step 1: Full test suite**

Run: `DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/autoagent .venv/bin/python3 -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 2: Lint**

Run: `ruff check .`
Expected: no errors.

- [ ] **Step 3: Format check**

Run: `ruff format --check .`
Expected: no diff. (If diff, run `ruff format .` then re-stage.)

- [ ] **Step 4: Manual smoke (documented in PR body, not run during this plan)**

In the PR description, include the following manual check that the reviewer must perform on staging:

> 1. DM the bot "create a test task for the cardamon repo".
> 2. Answer two clarifying questions.
> 3. `docker compose restart auto-agent` while the conversation is mid-flight.
> 4. Send the next answer.
> 5. **Expected:** the bot continues the drafting conversation; does not say "I don't have any prior context". If active tasks exist, it shows the picker instead — pick the draft via `new` to confirm draft persistence is wired.

- [ ] **Step 5: Final commit if any auto-format changes**

```bash
git status
# if any unstaged formatting changes:
git add -A
git commit -m "chore(lint): post-implementation format pass"
```

- [ ] **Step 6: Create PR (manual step)**

```bash
gh pr create --title "feat(slack): durable per-user conversation continuity" --body "$(cat <<'EOF'
## Summary
- Replace in-memory Slack DM session with durable Postgres-backed conversation history.
- Add per-user 24h focus pointer (source-agnostic; extends to Telegram).
- Add deterministic, stateless picker for switching/picking tasks.
- Rebind draft conversations to tasks on create_task; preserves "why this task exists" trail.

## Test plan
- [ ] All unit tests pass.
- [ ] Apply migration on staging; downgrade + upgrade clean.
- [ ] Manual smoke (per plan T8 Step 4) — mid-conversation restart preserves context.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review checklist for the implementing agent

After completing all tasks, verify against the spec:

- [ ] Migration creates both tables with correct columns + index + unique constraint.
- [ ] `MessengerConversation` and `UserFocus` ORM mappings match the migration.
- [ ] `messenger_router.handle` covers: thread fast path, focus expiry, picker resolution, picker render, switch keywords, reset, normal LLM dispatch, draft→task rebind.
- [ ] `converse` no longer references `_sessions` and accepts `history` + `on_create_task`.
- [ ] `integrations/slack/main.py` is the only call site for the top-level DM path.
- [ ] No usage of Redis in the router module (per the deviation noted in the plan header).
- [ ] History capped at 200 messages on every append.
- [ ] All tests pass with `DATABASE_URL` set.
