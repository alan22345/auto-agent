"""HTTP-flow tests for signup, email verification, and per-user secrets.

Mocks the session and the Resend dispatch — pgcrypto SQL is unit-tested in
``tests/test_secrets.py``. End-to-end pgcrypto round-trip is exercised in the
manual smoke (see plan section 9).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, Request, Response
from httpx import ASGITransport, AsyncClient

from orchestrator.auth import hash_password, verify_token
from orchestrator.router import COOKIE_NAME
from orchestrator.router import router as api_router
from shared.database import get_session
from shared.models import Organization, OrganizationMembership, Plan, User

# ---------------------------------------------------------------------------
# App scaffolding (mirrors tests/test_auth_cookie.py)
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        exempt = (
            "/api/auth/login",
            "/api/auth/logout",
            "/api/auth/signup",
            "/api/auth/verify",
        )
        if any(request.url.path.startswith(p) for p in exempt):
            return await call_next(request)
        cookie = request.cookies.get(COOKIE_NAME)
        if cookie and verify_token(cookie):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer ") and verify_token(auth[7:]):
            return await call_next(request)
        return Response(status_code=401, content="Authentication required")

    app.include_router(api_router, prefix="/api")
    return app


# ---------------------------------------------------------------------------
# Session stubs
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, value=None, rows=()):
        self._value = value
        self._rows = rows

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value

    def all(self):
        return list(self._rows)


class _SessionStub:
    """Programmable AsyncSession stub. Pre-load `responses` with the values
    each successive ``execute`` call should return (in order)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.added = []
        self.commits = 0
        self.flushes = 0
        self.refreshes = 0
        self._last_user: User | None = None
        self._last_org: Organization | None = None

    async def execute(self, *_a, **_kw):
        if not self._responses:
            return _Result()
        return self._responses.pop(0)

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, User):
            self._last_user = obj
        elif isinstance(obj, Organization):
            self._last_org = obj

    async def flush(self):
        # SQLAlchemy assigns autoincrement PKs during flush; the production
        # signup path now relies on this so the org has an ``id`` before the
        # membership row references it.
        self.flushes += 1
        if self._last_org is not None and getattr(self._last_org, "id", None) is None:
            self._last_org.id = 7
        if self._last_user is not None and getattr(self._last_user, "id", None) is None:
            self._last_user.id = 99

    async def commit(self):
        self.commits += 1
        # Simulate primary-key allocation on the most-recently-added User
        if self._last_user is not None and getattr(self._last_user, "id", None) is None:
            self._last_user.id = 99
        if self._last_org is not None and getattr(self._last_org, "id", None) is None:
            self._last_org.id = 7

    async def refresh(self, obj):
        self.refreshes += 1
        if isinstance(obj, User) and obj.id is None:
            obj.id = 99

    async def close(self):
        pass


def _override(app: FastAPI, sessions):
    """Yield each session in turn — one per HTTP request."""
    iterator = iter(sessions)

    async def _gen():
        yield next(iterator)

    app.dependency_overrides[get_session] = _gen


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------


async def test_signup_creates_user_and_dispatches_email():
    app = _build_app()
    # Three execute calls during signup: collision-check (no row) +
    # username-allocation lookup (no row) + free-plan lookup (Phase 4).
    # After Phase 2 the signup also adds Organization + OrganizationMembership
    # alongside the User row, but those don't trigger an execute.
    free_plan = MagicMock(spec=Plan)
    free_plan.id = 1
    free_plan.name = "free"
    session = _SessionStub([_Result(None), _Result(None), _Result(free_plan)])
    _override(app, [session])

    sent = []

    async def _fake_send(to, token):
        sent.append((to, token))

    with patch("shared.email.send_verification_email", new=_fake_send):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/api/auth/signup",
                json={
                    "email": "Alice@example.com",
                    "password": "hunter2hunter2",
                    "display_name": "Alice",
                },
            )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "alice@example.com"
    assert body["verification_sent"] is True
    assert body["user_id"] == 99
    # Phase 2 — signup creates Organization + User + OrganizationMembership.
    types_added = [type(o).__name__ for o in session.added]
    assert types_added == ["Organization", "User", "OrganizationMembership"]
    org, user, membership = session.added
    assert user.email == "alice@example.com"
    assert user.display_name == "Alice"
    assert user.signup_token is not None and len(user.signup_token) >= 32
    assert user.email_verified_at is None
    assert org.name == "Alice"
    assert org.slug.startswith("alice")
    assert membership.role == "owner"
    assert membership.user_id == user.id
    assert membership.org_id == org.id
    # Email dispatched with the same token that's now on the user row.
    assert len(sent) == 1
    assert sent[0] == ("alice@example.com", user.signup_token)


async def test_signup_rejects_invalid_email():
    app = _build_app()
    _override(app, [_SessionStub([])])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/api/auth/signup",
            json={"email": "not-an-email", "password": "hunter2hunter2", "display_name": "x"},
        )
    assert r.status_code == 400


async def test_signup_rejects_short_password():
    app = _build_app()
    _override(app, [_SessionStub([])])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/api/auth/signup",
            json={"email": "a@b.co", "password": "short", "display_name": "x"},
        )
    # Pydantic field validator returns 422.
    assert r.status_code == 422


async def test_signup_conflicts_on_duplicate_email():
    app = _build_app()
    existing = MagicMock(spec=User)
    existing.id = 1
    session = _SessionStub([_Result(existing)])
    _override(app, [session])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/api/auth/signup",
            json={"email": "a@b.co", "password": "hunter2hunter2", "display_name": "x"},
        )
    assert r.status_code == 409


async def test_signup_succeeds_when_email_dispatch_fails():
    """Resend outage shouldn't block account creation. The verify URL is
    logged so the operator can recover."""
    app = _build_app()
    free_plan = MagicMock(spec=Plan)
    free_plan.id = 1
    free_plan.name = "free"
    session = _SessionStub([_Result(None), _Result(None), _Result(free_plan)])
    _override(app, [session])

    async def _boom(to, token):
        raise RuntimeError("resend down")

    with patch("shared.email.send_verification_email", new=_boom):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(
                "/api/auth/signup",
                json={"email": "a@b.co", "password": "hunter2hunter2", "display_name": "x"},
            )
    assert r.status_code == 201
    assert r.json()["verification_sent"] is False


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


async def test_verify_token_marks_email_verified_and_sets_cookie():
    app = _build_app()
    user = MagicMock(spec=User)
    user.id = 7
    user.username = "alice"
    user.email = "a@b.co"
    user.email_verified_at = None
    user.signup_token = "tok123"
    # Two executes during verify: lookup user by token, then resolve
    # the user's active org membership.
    membership = MagicMock(spec=OrganizationMembership)
    membership.org_id = 11
    membership.user_id = 7
    membership.last_active_at = None
    session = _SessionStub([_Result(user), _Result(membership)])
    _override(app, [session])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/auth/verify/tok123")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["user_id"] == 7
    assert COOKIE_NAME in r.cookies
    assert user.email_verified_at is not None
    assert user.signup_token is None


async def test_verify_token_unknown_returns_404():
    app = _build_app()
    session = _SessionStub([_Result(None)])
    _override(app, [session])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/auth/verify/nope")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Login gating on email verification
# ---------------------------------------------------------------------------


def _make_user(*, email=None, email_verified_at=None, password="pw") -> MagicMock:
    u = MagicMock(spec=User)
    u.id = 1
    u.username = "alice"
    u.password_hash = hash_password(password)
    u.display_name = "Alice"
    u.created_at = None
    u.last_login = None
    u.claude_auth_status = "never_paired"
    u.claude_paired_at = None
    u.telegram_chat_id = None
    u.slack_user_id = None
    u.email = email
    u.email_verified_at = email_verified_at
    return u


async def test_login_blocked_when_email_unverified():
    app = _build_app()
    user = _make_user(email="a@b.co", email_verified_at=None)
    session = _SessionStub([_Result(user)])
    _override(app, [session])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/api/auth/login",
            json={"username": "a@b.co", "password": "pw"},
        )
    assert r.status_code == 403
    assert "verif" in r.json()["detail"].lower()


async def test_login_succeeds_for_verified_email():
    app = _build_app()
    user = _make_user(
        email="a@b.co",
        email_verified_at=datetime.now(UTC),
    )
    session = _login_session(user)
    _override(app, [session])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/api/auth/login",
            json={"username": "a@b.co", "password": "pw"},
        )
    assert r.status_code == 200


async def test_login_succeeds_for_legacy_user_without_email():
    """Admin/seeded users (email=NULL) bypass verification gating."""
    app = _build_app()
    user = _make_user(email=None, email_verified_at=None)
    session = _login_session(user)
    _override(app, [session])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/api/auth/login",
            json={"username": "alice", "password": "pw"},
        )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Per-user secrets API (cross-user isolation)
# ---------------------------------------------------------------------------


def _login_session(user, *, org_id: int = 1):
    """Pre-load a session for one /auth/login round-trip.

    Login does two executes after Phase 2: user lookup + membership lookup.
    """
    membership = MagicMock(spec=OrganizationMembership)
    membership.org_id = org_id
    membership.user_id = user.id
    membership.last_active_at = None
    return _SessionStub([_Result(user), _Result(membership)])


async def _login(c, user, session):
    """Drive a login round-trip and return the session cookie value."""
    r = await c.post("/api/auth/login", json={"username": user.username, "password": "pw"})
    assert r.status_code == 200, r.text
    return r.cookies[COOKIE_NAME]


async def test_secret_put_calls_secrets_set_with_caller_user_id():
    app = _build_app()
    user_a = _make_user(email=None)
    user_a.id = 11
    user_a.username = "ua"

    # First request = login, second = PUT.
    _override(app, [_login_session(user_a), _SessionStub([])])

    captured = {}

    async def _fake_set(uid, key, value, *, org_id=None, session=None):
        captured["uid"] = uid
        captured["org_id"] = org_id
        captured["key"] = key
        captured["value"] = value

    with patch("shared.secrets.set", new=_fake_set):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            cookie = await _login(c, user_a, None)
            r = await c.put(
                "/api/me/secrets/github_pat",
                json={"value": "ghp_xxx"},
                cookies={COOKIE_NAME: cookie},
            )
    assert r.status_code == 200
    assert captured == {
        "uid": 11, "org_id": 1, "key": "github_pat", "value": "ghp_xxx",
    }


async def test_unknown_secret_key_returns_404():
    app = _build_app()
    user = _make_user(email=None)
    user.id = 1
    _override(app, [_login_session(user), _SessionStub([])])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        cookie = await _login(c, user, None)
        r = await c.put(
            "/api/me/secrets/totally_made_up",
            json={"value": "x"},
            cookies={COOKIE_NAME: cookie},
        )
    assert r.status_code == 404


async def test_secret_list_uses_caller_user_id():
    """User A listing /me/secrets gets only user A's keys — never B's."""
    app = _build_app()
    user_a = _make_user(email=None)
    user_a.id = 11
    user_a.username = "ua"
    _override(app, [_login_session(user_a), _SessionStub([])])

    captured = {}

    async def _fake_list_keys(uid, *, org_id=None, session=None):
        captured["uid"] = uid
        captured["org_id"] = org_id
        return ["github_pat"]

    with patch("shared.secrets.list_keys", new=_fake_list_keys):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            cookie = await _login(c, user_a, None)
            r = await c.get("/api/me/secrets", cookies={COOKIE_NAME: cookie})
    assert r.status_code == 200
    assert r.json() == {"keys": ["github_pat"]}
    assert captured["uid"] == 11
    assert captured["org_id"] == 1
