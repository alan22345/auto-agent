"""Search tab API: sessions CRUD + streaming message endpoint."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import desc, select

from agent.search_loop import run_search_turn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession
from agent.search_title import generate_title
from orchestrator.auth import verify_token
from shared.config import settings
from shared.database import async_session, get_session
from shared.models import SearchMessage, SearchSession, User

router = APIRouter()
_COOKIE_NAME = "auto_agent_session"


def _verify_cookie_or_header(cookie: str | None, authorization: str | None) -> dict:
    if cookie:
        payload = verify_token(cookie)
        if payload:
            return payload
    if authorization and authorization.startswith("Bearer "):
        payload = verify_token(authorization[7:])
        if payload:
            return payload
    raise HTTPException(status_code=401, detail="Not authenticated")


def _current_user_id(
    authorization: str | None = Header(None),
    auto_agent_session: str | None = Cookie(default=None),
) -> int:
    return _verify_cookie_or_header(auto_agent_session, authorization)["user_id"]


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
    created_at: str


class SessionDetail(SessionData):
    messages: list[MessageData]


class SendMessageRequest(BaseModel):
    content: str


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
    user_id: int = Depends(_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> SessionData:
    s = SearchSession(user_id=user_id, title="New search")
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return _serialize_session(s)


@router.get("/search/sessions", response_model=list[SessionData])
async def list_sessions(
    user_id: int = Depends(_current_user_id),
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
    user_id: int = Depends(_current_user_id),
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
                created_at=m.created_at.isoformat(),
            )
            for m in msgs
        ],
    )


@router.delete("/search/sessions/{session_id}")
async def delete_session(
    session_id: int,
    user_id: int = Depends(_current_user_id),
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
    user_id: int = Depends(_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
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

    user_msg = SearchMessage(session_id=session_id, role="user", content=req.content)
    session.add(user_msg)
    await session.commit()

    history = await _load_history(session, session_id)
    is_first_user_message = sum(1 for h in history if h["role"] == "user") == 1
    content = req.content

    async def stream() -> AsyncIterator[bytes]:
        events_captured: list[dict] = []
        final_answer: str | None = None
        truncated = False
        try:
            async for ev in run_search_turn(
                user_message=content,
                history=history[:-1],
                brave_api_key=settings.brave_api_key,
                author=author,
            ):
                events_captured.append(ev)
                if ev["type"] == "done":
                    final_answer = ev.get("answer", "") or ""
                elif ev["type"] == "error":
                    truncated = True
                yield (json.dumps(ev) + "\n").encode("utf-8")
        except Exception as e:
            truncated = True
            yield (json.dumps({"type": "error", "message": str(e)}) + "\n").encode(
                "utf-8"
            )
        finally:
            # Persist only meaningful turns. An empty answer with no captured
            # events means the client disconnected before anything happened —
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
                )
                s2.add(msg)
                target = (
                    await s2.execute(
                        select(SearchSession).where(SearchSession.id == session_id)
                    )
                ).scalar_one()
                if is_first_user_message and target.title == "New search":
                    target.title = await generate_title(content)
                # Touch updated_at so the row's timestamp reflects this turn.
                target.updated_at = datetime.now(UTC)
                await s2.commit()

    return StreamingResponse(stream(), media_type="application/x-ndjson")
