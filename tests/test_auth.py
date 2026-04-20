"""Tests for authentication — password hashing and JWT tokens."""

import time

from orchestrator.auth import create_token, hash_password, verify_password, verify_token


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
        token = create_token(user_id=1, username="alice")
        payload = verify_token(token)
        assert payload is not None
        assert payload["user_id"] == 1
        assert payload["username"] == "alice"

    def test_invalid_token_returns_none(self):
        assert verify_token("garbage.token.here") is None

    def test_expired_token_returns_none(self):
        token = create_token(user_id=1, username="alice", expires_seconds=0)
        time.sleep(1)
        assert verify_token(token) is None
