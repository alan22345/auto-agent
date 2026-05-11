"""Per-user, per-org encrypted secrets store.

Backed by Postgres ``pgcrypto``. Plaintext is bound as a query parameter and
encrypted/decrypted by SQL — it never lives in a long-running Python variable
beyond the duration of the call.

Allowed keys are defined by ``SECRET_KEYS``. Adding a new secret kind requires
updating that constant and the per-key validation/test endpoints in
``orchestrator/router.py``.

Phase 2 — the primary key extended from ``(user_id, key)`` to
``(user_id, organization_id, key)``. A user in two orgs can hold different
credentials per org (e.g. a personal GitHub PAT in their solo org, an
employer-issued PAT in their work org). All callers must now pass ``org_id``.

Each helper accepts an optional ``session`` so callers already inside a
transaction can reuse it; otherwise a one-shot session is opened.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import settings
from shared.database import async_session

# Closed allowlist. The router whitelists path params against this set.
SECRET_KEYS: frozenset[str] = frozenset({"github_pat", "anthropic_api_key"})


class UnknownSecretKey(ValueError):
    """Raised when a caller passes a key not in ``SECRET_KEYS``."""


def _check_key(key: str) -> None:
    if key not in SECRET_KEYS:
        raise UnknownSecretKey(f"unknown secret key: {key!r}")


def _passphrase() -> str:
    p = settings.secrets_passphrase
    if not p:
        # Fail loud — silently encrypting with an empty passphrase would let
        # anyone with read access to the DB recover plaintext.
        raise RuntimeError(
            "SECRETS_PASSPHRASE is not configured; refusing to read or write encrypted secrets"
        )
    return p


async def _with_session(session: AsyncSession | None):
    if session is not None:
        return session, False
    return async_session(), True


async def set(
    user_id: int,
    key: str,
    value: str,
    *,
    org_id: int,
    session: AsyncSession | None = None,
) -> None:
    """Upsert ``user_id, org_id, key`` with the given plaintext value."""
    _check_key(key)
    passphrase = _passphrase()
    sess, owns = await _with_session(session)
    try:
        await sess.execute(
            text(
                "INSERT INTO user_secrets (user_id, organization_id, key, value_enc, updated_at) "
                "VALUES (:uid, :oid, :k, pgp_sym_encrypt(:v, :p), now()) "
                "ON CONFLICT (user_id, organization_id, key) DO UPDATE "
                "SET value_enc = pgp_sym_encrypt(:v, :p), updated_at = now()"
            ),
            {"uid": user_id, "oid": org_id, "k": key, "v": value, "p": passphrase},
        )
        if owns:
            await sess.commit()
    finally:
        if owns:
            await sess.close()


async def get(
    user_id: int,
    key: str,
    *,
    org_id: int,
    session: AsyncSession | None = None,
) -> str | None:
    """Return the plaintext value or ``None`` if no such row."""
    _check_key(key)
    passphrase = _passphrase()
    sess, owns = await _with_session(session)
    try:
        row = await sess.execute(
            text(
                "SELECT pgp_sym_decrypt(value_enc, :p)::text "
                "FROM user_secrets "
                "WHERE user_id = :uid AND organization_id = :oid AND key = :k"
            ),
            {"uid": user_id, "oid": org_id, "k": key, "p": passphrase},
        )
        return row.scalar_one_or_none()
    finally:
        if owns:
            await sess.close()


async def delete(
    user_id: int,
    key: str,
    *,
    org_id: int,
    session: AsyncSession | None = None,
) -> None:
    _check_key(key)
    sess, owns = await _with_session(session)
    try:
        await sess.execute(
            text(
                "DELETE FROM user_secrets "
                "WHERE user_id = :uid AND organization_id = :oid AND key = :k"
            ),
            {"uid": user_id, "oid": org_id, "k": key},
        )
        if owns:
            await sess.commit()
    finally:
        if owns:
            await sess.close()


async def list_keys(
    user_id: int,
    *,
    org_id: int,
    session: AsyncSession | None = None,
) -> list[str]:
    """Return the names of secrets set for this user in this org. Never returns values."""
    sess, owns = await _with_session(session)
    try:
        rows = await sess.execute(
            text(
                "SELECT key FROM user_secrets "
                "WHERE user_id = :uid AND organization_id = :oid "
                "ORDER BY key"
            ),
            {"uid": user_id, "oid": org_id},
        )
        return [r[0] for r in rows.all()]
    finally:
        if owns:
            await sess.close()
