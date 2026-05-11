"""Authentication utilities — password hashing and JWT tokens."""

from __future__ import annotations

import os
import time

import bcrypt
import jwt
from fastapi import Cookie, Depends, Header, HTTPException
from sqlalchemy import select

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
    *,
    current_org_id: int,
    expires_seconds: int = DEFAULT_EXPIRY,
) -> str:
    """Create a JWT token.

    ``current_org_id`` is required from Phase 2 onward — every authenticated
    request must know which tenant it operates against. Callers that pre-date
    Phase 2 will fail at this signature change, which is intentional: a
    silent default would be a security hole.
    """
    payload = {
        "user_id": user_id,
        "username": username,
        "current_org_id": current_org_id,
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


def current_org_id(
    authorization: str | None = Header(None),
    auto_agent_session: str | None = Cookie(default=None),
) -> int:
    """FastAPI dependency that extracts the user's active organization_id.

    Raises 401 if the token has no ``current_org_id`` — that means it was
    issued by pre-Phase-2 code and the user must re-authenticate. Failing
    loud here prevents a stale session from inadvertently bypassing
    org-scoped queries.
    """
    payload = verify_cookie_or_header(auto_agent_session, authorization)
    org_id = payload.get("current_org_id")
    if org_id is None:
        raise HTTPException(
            status_code=401,
            detail="Session predates the org model — please log in again",
        )
    return int(org_id)


async def _role_in_org(*, user_id: int, org_id: int) -> str | None:
    """Look up a user's role in an organization."""
    from shared.database import async_session
    from shared.models import OrganizationMembership

    async with async_session() as session:
        result = await session.execute(
            select(OrganizationMembership.role).where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.org_id == org_id,
            )
        )
        return result.scalar_one_or_none()


async def current_org_id_admin_dep(
    user_id: int = Depends(current_user_id),
    org_id: int = Depends(current_org_id),
) -> int:
    """Require the caller to be `owner` or `admin` of the current org.

    Raises 403 if the user does not have owner or admin role.
    """
    role = await _role_in_org(user_id=user_id, org_id=org_id)
    if role not in ("owner", "admin"):
        raise HTTPException(403, "Org admin role required")
    return org_id
