"""Tests for authentication — password hashing and JWT tokens."""

import time

import jwt
import pytest
from fastapi import HTTPException

from orchestrator.auth import (
    JWT_SECRET,
    create_token,
    current_org_id,
    hash_password,
    verify_password,
    verify_token,
)


class TestPasswordHashing:
    def test_hash_and_verify(self):
        hashed = hash_password("secret123")
        assert hashed != "secret123"
        assert verify_password("secret123", hashed)

    def test_wrong_password_fails(self):
        hashed = hash_password("secret123")
        assert not verify_password("wrong", hashed)

    def test_different_hashes_for_same_password(self):
        h1 = hash_password("secret123")
        h2 = hash_password("secret123")
        assert h1 != h2  # bcrypt uses random salt


class TestJWT:
    def test_create_and_verify(self):
        token = create_token(user_id=1, username="alice", current_org_id=42)
        payload = verify_token(token)
        assert payload is not None
        assert payload["user_id"] == 1
        assert payload["username"] == "alice"
        assert payload["current_org_id"] == 42

    def test_invalid_token_returns_none(self):
        assert verify_token("garbage.token.here") is None

    def test_expired_token_returns_none(self):
        token = create_token(
            user_id=1, username="alice", current_org_id=1, expires_seconds=0,
        )
        time.sleep(1)
        assert verify_token(token) is None

    def test_create_token_requires_current_org_id(self):
        """Legacy callers without current_org_id fail loud (security).

        Silent default would be a tenant-leak vector.
        """
        with pytest.raises(TypeError):
            create_token(user_id=1, username="alice")  # type: ignore[call-arg]


class TestCurrentOrgIdDep:
    def test_returns_org_from_valid_cookie(self):
        token = create_token(user_id=1, username="alice", current_org_id=7)
        assert current_org_id(authorization=None, auto_agent_session=token) == 7

    def test_returns_org_from_bearer_header(self):
        token = create_token(user_id=1, username="alice", current_org_id=99)
        assert current_org_id(
            authorization=f"Bearer {token}", auto_agent_session=None,
        ) == 99

    def test_rejects_legacy_token_without_org(self):
        """A pre-Phase-2 token without current_org_id is 401."""
        legacy = jwt.encode(
            {"user_id": 1, "username": "alice", "exp": 2_000_000_000},
            JWT_SECRET, algorithm="HS256",
        )
        with pytest.raises(HTTPException) as exc:
            current_org_id(authorization=None, auto_agent_session=legacy)
        assert exc.value.status_code == 401

    def test_rejects_no_token(self):
        with pytest.raises(HTTPException) as exc:
            current_org_id(authorization=None, auto_agent_session=None)
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_current_org_id_admin_dep_blocks_member(monkeypatch):
    """A user with role='member' on an org gets 403 from the admin dep."""
    from orchestrator.auth import current_org_id_admin_dep

    async def fake_role(*, user_id, org_id):
        return "member"

    monkeypatch.setattr(
        "orchestrator.auth._role_in_org", fake_role, raising=False
    )

    with pytest.raises(HTTPException) as exc:
        await current_org_id_admin_dep(user_id=5, org_id=10)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_current_org_id_admin_dep_allows_owner(monkeypatch):
    from orchestrator.auth import current_org_id_admin_dep

    async def fake_role(*, user_id, org_id):
        return "owner"

    monkeypatch.setattr(
        "orchestrator.auth._role_in_org", fake_role, raising=False
    )
    out = await current_org_id_admin_dep(user_id=5, org_id=10)
    assert out == 10


@pytest.mark.asyncio
async def test_current_org_id_admin_dep_allows_admin(monkeypatch):
    from orchestrator.auth import current_org_id_admin_dep

    async def fake_role(*, user_id, org_id):
        return "admin"

    monkeypatch.setattr(
        "orchestrator.auth._role_in_org", fake_role, raising=False
    )
    out = await current_org_id_admin_dep(user_id=5, org_id=10)
    assert out == 10
