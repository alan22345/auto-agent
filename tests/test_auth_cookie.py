"""Cookie-based auth on /api/auth/* endpoints.

Uses a minimal FastAPI app (no background workers) that includes only the
orchestrator router, with the DB session dependency overridden to use mocks.
This tests the HTTP-level cookie behaviour without a real database.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI, Request, Response
from httpx import ASGITransport, AsyncClient

from orchestrator.auth import hash_password, verify_token
from orchestrator.router import router as api_router
from shared.database import get_session
from shared.models import OrganizationMembership, User

COOKIE_NAME = "auto_agent_session"


# ---------------------------------------------------------------------------
# Minimal test app — just the orchestrator router + cookie-aware middleware
# ---------------------------------------------------------------------------

test_app = FastAPI()


@test_app.middleware("http")
async def cookie_auth_middleware(request: Request, call_next):
    """Mirror of run.py's jwt_auth_middleware — also accepts the session cookie."""
    exempt_prefixes = ("/api/auth/login", "/api/auth/logout")
    if any(request.url.path.startswith(p) for p in exempt_prefixes):
        return await call_next(request)

    # Bearer header
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        payload = verify_token(auth[7:])
        if payload:
            return await call_next(request)

    # Cookie
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        payload = verify_token(cookie)
        if payload:
            return await call_next(request)

    return Response(status_code=401, content="Authentication required")


test_app.include_router(api_router, prefix="/api")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(user_id: int = 1, username: str = "cookie_user") -> MagicMock:
    u = MagicMock(spec=User)
    u.id = user_id
    u.username = username
    u.password_hash = hash_password("pw")
    u.display_name = username
    u.created_at = None
    u.last_login = None
    u.claude_auth_status = "never_paired"
    u.claude_paired_at = None
    u.telegram_chat_id = None
    u.slack_user_id = None
    return u


def _mock_session_for_login(user: MagicMock):
    """Return an async session mock for /auth/login.

    Login does two executes now: lookup the user, then resolve the user's
    active org membership. The second execute returns a fake membership
    with org_id=1.
    """
    session = AsyncMock()
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = user

    membership = MagicMock(spec=OrganizationMembership)
    membership.org_id = 1
    membership.user_id = user.id
    membership.last_active_at = None
    membership_result = MagicMock()
    membership_result.scalar_one_or_none.return_value = membership

    session.execute = AsyncMock(side_effect=[user_result, membership_result])
    session.commit = AsyncMock()
    return session


def _mock_session_for_me(user: MagicMock):
    """Return an async session mock that returns *user* on execute."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    session.execute = AsyncMock(return_value=result)
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_login_sets_cookie():
    user = _make_user()
    session = _mock_session_for_login(user)

    async def _override():
        yield session

    test_app.dependency_overrides[get_session] = _override
    try:
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
            r = await c.post("/api/auth/login", json={"username": "cookie_user", "password": "pw"})
        assert r.status_code == 200
        assert COOKIE_NAME in r.cookies
    finally:
        test_app.dependency_overrides.clear()


async def test_me_accepts_cookie():
    user = _make_user()
    login_session = _mock_session_for_login(user)
    me_session = _mock_session_for_me(user)
    call_count = 0

    async def _override():
        nonlocal call_count
        if call_count == 0:
            call_count += 1
            yield login_session
        else:
            yield me_session

    test_app.dependency_overrides[get_session] = _override
    try:
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
            login = await c.post(
                "/api/auth/login", json={"username": "cookie_user", "password": "pw"}
            )
            assert login.status_code == 200
            token = login.cookies[COOKIE_NAME]

            r = await c.get("/api/auth/me", cookies={COOKIE_NAME: token})
            assert r.status_code == 200
            assert r.json()["username"] == "cookie_user"
    finally:
        test_app.dependency_overrides.clear()


async def test_logout_clears_cookie():
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as c:
        r = await c.post("/api/auth/logout")
        assert r.status_code == 200
        assert r.cookies.get(COOKIE_NAME, "") == ""
