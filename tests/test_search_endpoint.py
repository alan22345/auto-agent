"""Tests for the search tab API (orchestrator/search.py).

Mirrors the dependency-override pattern in test_auth_cookie.py: builds a
minimal FastAPI app, overrides shared.database.get_session with an
AsyncMock, and exercises the endpoints over httpx.AsyncClient.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orchestrator.auth import create_token, current_user_id
from orchestrator.search import router as search_router
from shared.database import get_session
from shared.models import SearchSession, User


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(search_router, prefix="/api")
    return app


def _bearer(user_id: int = 1, username: str = "alice") -> dict[str, str]:
    return {"Authorization": f"Bearer {create_token(user_id, username)}"}


# ---------------------------------------------------------------------------
# Test 1: create session persists a SearchSession row
# ---------------------------------------------------------------------------


async def test_create_session_persists():
    added: list[object] = []

    session = AsyncMock()
    session.add = MagicMock(side_effect=lambda obj: added.append(obj))
    session.commit = AsyncMock()

    async def _refresh(obj):
        # Populate fields the response_model needs
        obj.id = 42
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        obj.created_at = now
        obj.updated_at = now

    session.refresh = AsyncMock(side_effect=_refresh)

    async def _override():
        yield session

    app = _make_app()
    app.dependency_overrides[get_session] = _override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.post(
                "/api/search/sessions", json={}, headers=_bearer(user_id=7)
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == 42
        assert body["title"] == "New search"
        # session.add was called with a SearchSession scoped to user 7
        assert len(added) == 1
        assert isinstance(added[0], SearchSession)
        assert added[0].user_id == 7
        session.commit.assert_awaited()
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Test 2: send_message returns 503 when brave_api_key is unset
# ---------------------------------------------------------------------------


async def test_send_message_503_when_brave_unset():
    session = AsyncMock()

    async def _override():
        yield session

    app = _make_app()
    app.dependency_overrides[get_session] = _override
    try:
        with patch("orchestrator.search.settings") as mock_settings:
            mock_settings.brave_api_key = ""
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.post(
                    "/api/search/sessions/1/messages",
                    json={"content": "hello"},
                    headers=_bearer(),
                )
        assert r.status_code == 503
        assert "BRAVE_API_KEY" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Test 3: send_message streams events and persists the assistant turn
# ---------------------------------------------------------------------------


async def test_send_message_streams_events_and_persists():
    # Mock SearchSession row owned by user 1
    sess_row = MagicMock(spec=SearchSession)
    sess_row.id = 1
    sess_row.user_id = 1
    sess_row.title = "New search"

    user_row = MagicMock(spec=User)
    user_row.id = 1
    user_row.username = "alice"

    # Order of executes inside send_message:
    # 1. SELECT SearchSession (sess lookup)
    # 2. SELECT User
    # 3. SELECT SearchMessage (history)
    sess_result = MagicMock()
    sess_result.scalar_one_or_none.return_value = sess_row
    user_result = MagicMock()
    user_result.scalar_one.return_value = user_row
    # History after user message was just inserted: one user row.
    user_history_row = MagicMock()
    user_history_row.role = "user"
    user_history_row.content = "hello"
    history_result = MagicMock()
    history_scalars = MagicMock()
    history_scalars.all.return_value = [user_history_row]
    history_result.scalars.return_value = history_scalars

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[sess_result, user_result, history_result])
    session.add = MagicMock()
    session.commit = AsyncMock()

    async def _override():
        yield session

    # Mock the async_session() context manager used by the persistence
    # path. The endpoint opens this context twice: once to insert the
    # assistant SearchMessage row + bump SearchSession.updated_at, and a
    # second time to set the auto-generated title (separate transaction
    # so a slow title call can't block persistence).
    s2_added: list[object] = []
    s2 = AsyncMock()
    s2.add = MagicMock(side_effect=lambda obj: s2_added.append(obj))
    s2.commit = AsyncMock()
    target_session = MagicMock(spec=SearchSession)
    target_session.id = 1
    target_session.title = "New search"
    target_result = MagicMock()
    target_result.scalar_one.return_value = target_session
    target_result.scalar_one_or_none.return_value = target_session
    s2.execute = AsyncMock(return_value=target_result)

    @asynccontextmanager
    async def _async_session_cm():
        yield s2

    def _async_session_factory():
        return _async_session_cm()

    # Canned event stream from run_search_turn
    canned = [
        {"type": "tool_call_start", "tool": "web_search", "args": {"query": "x"}},
        {"type": "source", "url": "https://example.com", "title": "Ex"},
        {"type": "text", "delta": "Hello "},
        {"type": "text", "delta": "world."},
        {"type": "done", "answer": "Hello world."},
    ]

    async def fake_run_search_turn(**_kwargs):
        for ev in canned:
            yield ev

    async def fake_generate_title(_msg):
        return "Test Title"

    app = _make_app()
    app.dependency_overrides[get_session] = _override
    # Pin user_id to 1 so session ownership matches sess_row.user_id
    app.dependency_overrides[current_user_id] = lambda: 1

    try:
        with (
            patch("orchestrator.search.settings") as mock_settings,
            patch("orchestrator.search.run_search_turn", fake_run_search_turn),
            patch("orchestrator.search.async_session", _async_session_factory),
            patch("orchestrator.search.generate_title", fake_generate_title),
        ):
            mock_settings.brave_api_key = "fake-key"
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.post(
                    "/api/search/sessions/1/messages",
                    json={"content": "hello"},
                )
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("application/x-ndjson")

            lines = [ln for ln in r.text.split("\n") if ln.strip()]
            parsed = [json.loads(ln) for ln in lines]
            assert parsed == canned

            # Assistant message persisted via async_session()
            assert len(s2_added) == 1
            saved = s2_added[0]
            assert saved.role == "assistant"
            assert saved.content == "Hello world."
            assert saved.session_id == 1
            assert saved.truncated is False
            # Title was generated since this is the first user message
            assert target_session.title == "Test Title"
            s2.commit.assert_awaited()
    finally:
        app.dependency_overrides.clear()
