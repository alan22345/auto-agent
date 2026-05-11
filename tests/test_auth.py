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
