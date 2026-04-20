"""Authentication utilities — password hashing and JWT tokens."""

from __future__ import annotations

import os
import time

import bcrypt
import jwt

JWT_SECRET = os.environ.get("JWT_SECRET", "auto-agent-jwt-secret-change-me")
JWT_ALGORITHM = "HS256"
DEFAULT_EXPIRY = 7 * 24 * 3600  # 7 days


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
