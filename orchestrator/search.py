"""Search tab API: sessions CRUD + streaming message endpoint."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from agent.search_loop import run_search_turn
from agent.search_title import generate_title
from orchestrator.auth import current_user_id
from shared.config import settings
from shared.database import async_session, get_session
from shared.models import SearchMessage, SearchSession, User

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


# ---------- Schemas ----------


class CreateSessionRequest(BaseModel):
    pass


class SessionData(BaseModel):
    id: int
    title: str
    created_at: str
    updated_at: str


class MessageData(BaseModel):
    id: int
    role: str
    content: str
    tool_events: list
    truncated: bool
    input_tokens: int = 0
    output_tokens: int = 0
    created_at: str


class SessionDetail(SessionData):
    messages: list[MessageData]


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1)


class UpdateSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=512)


# ---------- Sessions CRUD ----------


def _serialize_session(s: SearchSession) -> SessionData:
    return SessionData(
        id=s.id,
        title=s.title,
        created_at=s.created_at.isoformat(),
        updated_at=s.updated_at.isoformat(),
    )


@router.post("/search/sessions", response_model=SessionData)
async def create_session(
    _req: CreateSessionRequest,
    user_id: int = Depends(current_user_id),
    session: AsyncSession = Depends(get_session),
) -> SessionData:
    s = SearchSession(user_id=user_id, title="New search")
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return _serialize_session(s)


@router.get("/search/sessions", response_model=list[SessionData])
async def list_sessions(
    user_id: int = Depends(current_user_id),
    session: AsyncSession = Depends(get_session),
) -> list[SessionData]:
    rows = (
        await session.execute(
            select(SearchSession)
            .where(SearchSession.user_id == user_id)
            .order_by(desc(SearchSession.updated_at))
        )
    ).scalars().all()
    return [_serialize_session(r) for r in rows]


@router.get("/search/sessions/{session_id}", response_model=SessionDetail)
async def get_session_detail(
    session_id: int,
    user_id: int = Depends(current_user_id),
    session: AsyncSession = Depends(get_session),
) -> SessionDetail:
    row = (
        await session.execute(
            select(SearchSession).where(
                SearchSession.id == session_id,
                SearchSession.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    msgs = (
        await session.execute(
            select(SearchMessage)
            .where(SearchMessage.session_id == session_id)
            .order_by(SearchMessage.id)
        )
    ).scalars().all()

    return SessionDetail(
        **_serialize_session(row).model_dump(),
        messages=[
            MessageData(
                id=m.id,
                role=m.role,
                content=m.content,
                tool_events=list(m.tool_events or []),
                truncated=m.truncated,
                input_tokens=m.input_tokens or 0,
                output_tokens=m.output_tokens or 0,
                created_at=m.created_at.isoformat(),
            )
            for m in msgs
        ],
    )


@router.patch("/search/sessions/{session_id}", response_model=SessionData)
async def update_session(
    session_id: int,
    req: UpdateSessionRequest,
    user_id: int = Depends(current_user_id),
    session: AsyncSession = Depends(get_session),
) -> SessionData:
    new_title = req.title.strip()
    if not new_title:
        raise HTTPException(status_code=422, detail="Title is required.")
    row = (
        await session.execute(
            select(SearchSession).where(
                SearchSession.id == session_id,
                SearchSession.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    row.title = new_title[:512]
    row.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(row)
    return _serialize_session(row)


@router.delete("/search/sessions/{session_id}")
async def delete_session(
    session_id: int,
    user_id: int = Depends(current_user_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = (
        await session.execute(
            select(SearchSession).where(
                SearchSession.id == session_id,
                SearchSession.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    await session.delete(row)
    await session.commit()
    return {"ok": True}


# ---------- Streaming ----------


async def _load_history(session: AsyncSession, session_id: int) -> list[dict]:
    rows = (
        await session.execute(
            select(SearchMessage)
            .where(SearchMessage.session_id == session_id)
            .order_by(SearchMessage.id)
        )
    ).scalars().all()
    return [{"role": r.role, "content": r.content} for r in rows]


@router.post("/search/sessions/{session_id}/messages")
async def send_message(
    session_id: int,
    req: SendMessageRequest,
    user_id: int = Depends(current_user_id),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    user_content = req.content.strip()
    if not user_content:
        raise HTTPException(status_code=422, detail="Message content is required.")
    if not settings.brave_api_key:
        raise HTTPException(
            status_code=503,
            detail="Search is not configured. Set BRAVE_API_KEY.",
        )

    sess = (
        await session.execute(
            select(SearchSession).where(
                SearchSession.id == session_id,
                SearchSession.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")

    user_row = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    author = user_row.username

    user_msg = SearchMessage(session_id=session_id, role="user", content=user_content)
    session.add(user_msg)
    await session.commit()

    history = await _load_history(session, session_id)
    is_first_user_message = sum(1 for h in history if h["role"] == "user") == 1
    content = user_content

    async def _persist_assistant(
        events_captured: list[dict],
        final_answer: str | None,
        input_tokens: int,
        output_tokens: int,
        truncated: bool,
    ) -> None:
        # Persist only meaningful turns. An empty answer with no captured
        # events means the stream ended before anything useful happened —
        # don't leave an orphan blank assistant row in the session.
        persisted_events = [
            e
            for e in events_captured
            if e.get("type") in ("source", "memory_hit")
        ]
        answer = final_answer or ""
        if not answer and not persisted_events and not truncated:
            return
        async with async_session() as s2:
            msg = SearchMessage(
                session_id=session_id,
                role="assistant",
                content=answer,
                tool_events=persisted_events,
                truncated=truncated,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            s2.add(msg)
            target = (
                await s2.execute(
                    select(SearchSession).where(SearchSession.id == session_id)
                )
            ).scalar_one()
            target.updated_at = datetime.now(UTC)
            await s2.commit()

    async def _maybe_set_title(content: str) -> None:
        if not is_first_user_message:
            return
        try:
            new_title = await generate_title(content)
        except Exception:
            return
        async with async_session() as s3:
            target = (
                await s3.execute(
                    select(SearchSession).where(
                        SearchSession.id == session_id,
                        SearchSession.title == "New search",
                    )
                )
            ).scalar_one_or_none()
            if target is not None:
                target.title = new_title
                await s3.commit()

    async def stream() -> AsyncIterator[bytes]:
        events_captured: list[dict] = []
        final_answer: str | None = None
        input_tokens = 0
        output_tokens = 0
        truncated = False
        persisted = False
        try:
            async for ev in run_search_turn(
                user_message=content,
                history=history[:-1],
                brave_api_key=settings.brave_api_key,
                author=author,
            ):
                events_captured.append(ev)
                if ev["type"] == "done":
                    # Capture totals, then commit the assistant row BEFORE
                    # yielding `done` to the client. The client refetches
                    # the session as soon as it sees `done`, so persistence
                    # must happen first or the assistant message will be
                    # invisible until the next reload.
                    final_answer = ev.get("answer", "") or ""
                    input_tokens = int(ev.get("input_tokens") or 0)
                    output_tokens = int(ev.get("output_tokens") or 0)
                    await _persist_assistant(
                        events_captured, final_answer, input_tokens, output_tokens, truncated,
                    )
                    persisted = True
                elif ev["type"] == "error":
                    truncated = True
                yield (json.dumps(ev) + "\n").encode("utf-8")
        except Exception as e:
            truncated = True
            yield (json.dumps({"type": "error", "message": str(e)}) + "\n").encode(
                "utf-8"
            )
        finally:
            # Catch-all: if we never reached the `done` branch (client
            # disconnected mid-stream, error event, etc.), still persist
            # whatever we captured.
            if not persisted:
                await _persist_assistant(
                    events_captured, final_answer, input_tokens, output_tokens, truncated,
                )
            await _maybe_set_title(content)

    return StreamingResponse(stream(), media_type="application/x-ndjson")
