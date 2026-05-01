"""Authentication utilities — password hashing and JWT tokens."""

from __future__ import annotations

import os
import time

import bcrypt
import jwt
from fastapi import Cookie, Header, HTTPException

JWT_SECRET = os.environ.get("JWT_SECRET", "auto-agent-jwt-secret-change-me")
JWT_ALGORITHM = "HS256"
DEFAULT_EXPIRY = 7 * 24 * 3600  # 7 days
COOKIE_NAME = "auto_agent_session"


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Check a password against a bcrypt hash."""
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_token(
    user_id: int,
    username: str,
    expires_seconds: int = DEFAULT_EXPIRY,
) -> str:
    """Create a JWT token."""
    payload = {
        "user_id": user_id,
        "username": username,
        "exp": int(time.time()) + expires_seconds,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> dict | None:
    """Verify and decode a JWT token. Returns payload dict or None."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def verify_cookie_or_header(cookie: str | None, authorization: str | None) -> dict:
    """Accept either the session cookie or `Authorization: Bearer <jwt>`.

    Raises 401 if neither carries a valid token.
    """
    if cookie:
        payload = verify_token(cookie)
        if payload:
            return payload
    if authorization and authorization.startswith("Bearer "):
        payload = verify_token(authorization[7:])
        if payload:
            return payload
    raise HTTPException(status_code=401, detail="Not authenticated")


def current_user_id(
    authorization: str | None = Header(None),
    auto_agent_session: str | None = Cookie(default=None),
) -> int:
    """FastAPI dependency that extracts the authenticated user_id."""
    return verify_cookie_or_header(auto_agent_session, authorization)["user_id"]
