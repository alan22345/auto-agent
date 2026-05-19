"""Per-repo encrypted project secrets store (ADR-019).

Backed by Postgres ``pgcrypto``. Plaintext is bound as a query parameter and
encrypted/decrypted by SQL — it never lives in a long-running Python variable
beyond the duration of the call.

Unlike ``shared/secrets.py``, this module accepts any free-form key matching
``^[A-Z][A-Z0-9_]*$`` — no closed allowlist. The whole point is to let the
domain architect declare any credential a project happens to need.

Two sources:
  - ``'user'`` — typed by the user; value_enc is always populated.
  - ``'architect_required'`` — declared by the domain architect; value_enc is
    nullable until the user populates it (allowing placeholder rows).

Each helper accepts an optional ``session`` so callers already inside a
transaction can reuse it; otherwise a one-shot session is opened.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import settings
from shared.database import async_session

# Free-form keys must be uppercase env-var-style names.
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _check_key(key: str) -> None:
    """Raise ValueError if key doesn't match the required format."""
    if not _KEY_RE.match(key):
        raise ValueError(
            f"invalid secret key {key!r}: must match ^[A-Z][A-Z0-9_]*$ "
            "(uppercase letters, digits, underscores; must start with a letter)"
        )
    if len(key) > 128:
        raise ValueError(f"invalid secret key {key!r}: exceeds 128 characters")


def _passphrase() -> str:
    p = settings.secrets_passphrase
    if not p:
        # Fail loud — silently encrypting with an empty passphrase would let
        # anyone with read access to the DB recover plaintext.
        raise RuntimeError(
            "SECRETS_PASSPHRASE is not configured; refusing to read or write encrypted secrets"
        )
    return p


async def _with_session(session: AsyncSession | None) -> tuple[AsyncSession, bool]:
    if session is not None:
        return session, False
    return async_session(), True


async def set(
    repo_id: int,
    key: str,
    value: str,
    *,
    organization_id: int,
    source: str = "user",
    purpose: str | None = None,
    session: AsyncSession | None = None,
) -> None:
    """Upsert ``(repo_id, key)`` with the given plaintext value.

    If the row already exists, updates ``value_enc`` and ``updated_at``.
    ``source`` and ``purpose`` are only written on INSERT; to change them
    on an existing row, call ``upsert_architect_required`` or ``demote_to_user``.
    """
    _check_key(key)
    passphrase = _passphrase()
    sess, owns = await _with_session(session)
    try:
        await sess.execute(
            text(
                "INSERT INTO repo_secrets "
                "  (repo_id, organization_id, key, value_enc, source, purpose, created_at, updated_at) "
                "VALUES "
                "  (:rid, :oid, :k, pgp_sym_encrypt(:v, :p), :src, :pur, now(), now()) "
                "ON CONFLICT (repo_id, key) DO UPDATE "
                "SET value_enc = pgp_sym_encrypt(:v, :p), updated_at = now()"
            ),
            {
                "rid": repo_id,
                "oid": organization_id,
                "k": key,
                "v": value,
                "p": passphrase,
                "src": source,
                "pur": purpose,
            },
        )
        if owns:
            await sess.commit()
    finally:
        if owns:
            await sess.close()


async def get(
    repo_id: int,
    key: str,
    *,
    organization_id: int,
    session: AsyncSession | None = None,
) -> str | None:
    """Return the plaintext value or ``None`` if no such row or value_enc is null."""
    _check_key(key)
    passphrase = _passphrase()
    sess, owns = await _with_session(session)
    try:
        row = await sess.execute(
            text(
                "SELECT pgp_sym_decrypt(value_enc, :p)::text "
                "FROM repo_secrets "
                "WHERE repo_id = :rid AND organization_id = :oid AND key = :k "
                "  AND value_enc IS NOT NULL"
            ),
            {"rid": repo_id, "oid": organization_id, "k": key, "p": passphrase},
        )
        value = row.scalar_one_or_none()
        if value is not None:
            # Register the plaintext so structlog redacts it from log events.
            # Imported lazily to avoid a circular import (shared.logging imports
            # shared.config, which is a peer module — no cycle — but lazy import
            # makes the dependency direction explicit).
            from shared.logging import register_secret

            register_secret(value)
        return value
    finally:
        if owns:
            await sess.close()


async def delete(
    repo_id: int,
    key: str,
    *,
    organization_id: int,
    session: AsyncSession | None = None,
) -> None:
    """Delete the row for ``(repo_id, organization_id, key)``."""
    _check_key(key)
    sess, owns = await _with_session(session)
    try:
        await sess.execute(
            text(
                "DELETE FROM repo_secrets "
                "WHERE repo_id = :rid AND organization_id = :oid AND key = :k"
            ),
            {"rid": repo_id, "oid": organization_id, "k": key},
        )
        if owns:
            await sess.commit()
    finally:
        if owns:
            await sess.close()


async def list_keys(
    repo_id: int,
    *,
    organization_id: int,
    session: AsyncSession | None = None,
) -> list[dict]:
    """Return ``[{key, set, source, purpose, updated_at}]`` for this repo.

    ``set`` is True when ``value_enc IS NOT NULL``. **Never returns values.**
    """
    sess, owns = await _with_session(session)
    try:
        result = await sess.execute(
            text(
                "SELECT key, "
                "       value_enc IS NOT NULL AS set, "
                "       source, "
                "       purpose, "
                "       updated_at "
                "FROM repo_secrets "
                "WHERE repo_id = :rid AND organization_id = :oid "
                "ORDER BY key"
            ),
            {"rid": repo_id, "oid": organization_id},
        )
        return [dict(row) for row in result.mappings().fetchall()]
    finally:
        if owns:
            await sess.close()


async def get_all_for_boot(
    repo_id: int,
    *,
    organization_id: int,
    session: AsyncSession | None = None,
) -> dict[str, str]:
    """Return ``{key: plaintext_value}`` for every row with value_enc IS NOT NULL.

    **Only ``boot_dev_server`` and the workspace .env writer call this.**
    Never called via the HTTP layer.
    """
    passphrase = _passphrase()
    sess, owns = await _with_session(session)
    try:
        result = await sess.execute(
            text(
                "SELECT key, pgp_sym_decrypt(value_enc, :p)::text AS plaintext "
                "FROM repo_secrets "
                "WHERE repo_id = :rid AND organization_id = :oid "
                "  AND value_enc IS NOT NULL"
            ),
            {"rid": repo_id, "oid": organization_id, "p": passphrase},
        )
        result_dict = {row[0]: row[1] for row in result.all()}
        # Register every plaintext value with the structlog redactor so any
        # accidental log of these strings (or strings containing them) is
        # automatically scrubbed.
        from shared.logging import register_secret

        for v in result_dict.values():
            if v:  # skip null/empty
                register_secret(v)
        return result_dict
    finally:
        if owns:
            await sess.close()


async def list_missing_architect_required(
    repo_id: int,
    *,
    organization_id: int,
    session: AsyncSession | None = None,
) -> list[str]:
    """Return keys where source='architect_required' AND value_enc IS NULL.

    Used by the scaffold gate (ADR-019 T7) to determine whether Phase D
    (child trio dispatch) can proceed. An empty list means the gate is
    satisfied — every declared requirement has been fulfilled.
    """
    sess, owns = await _with_session(session)
    try:
        result = await sess.execute(
            text(
                "SELECT key "
                "FROM repo_secrets "
                "WHERE repo_id = :rid AND organization_id = :oid "
                "  AND source = 'architect_required' "
                "  AND value_enc IS NULL "
                "ORDER BY key"
            ),
            {"rid": repo_id, "oid": organization_id},
        )
        return [row[0] for row in result.all()]
    finally:
        if owns:
            await sess.close()


async def upsert_architect_required(
    repo_id: int,
    key: str,
    purpose: str,
    *,
    organization_id: int,
    session: AsyncSession | None = None,
) -> None:
    """Promote a row to ``source='architect_required'`` and set ``purpose``.

    If the row doesn't exist, creates it with ``value_enc=NULL``.
    If the row exists with ``source='user'``, flips source and sets purpose
    while preserving the existing value.
    """
    _check_key(key)
    # Validate passphrase is configured (same guard as set/get). The upsert
    # for architect-required rows uses NULL value_enc, so no encryption is
    # needed here — but we still refuse to operate without a passphrase so
    # the security invariant is uniform across all write paths.
    _passphrase()
    sess, owns = await _with_session(session)
    try:
        await sess.execute(
            text(
                "INSERT INTO repo_secrets "
                "  (repo_id, organization_id, key, value_enc, source, purpose, created_at, updated_at) "
                "VALUES "
                "  (:rid, :oid, :k, NULL, 'architect_required', :pur, now(), now()) "
                "ON CONFLICT (repo_id, key) DO UPDATE "
                "SET source = 'architect_required', "
                "    purpose = :pur, "
                "    updated_at = now()"
            ),
            {
                "rid": repo_id,
                "oid": organization_id,
                "k": key,
                "pur": purpose,
            },
        )
        if owns:
            await sess.commit()
    finally:
        if owns:
            await sess.close()


async def demote_to_user(
    repo_id: int,
    key: str,
    *,
    organization_id: int,
    session: AsyncSession | None = None,
) -> None:
    """Flip ``source='architect_required'`` to ``'user'`` and clear ``purpose``.

    Preserves the existing ``value_enc`` — the user's value is not touched.
    If the row doesn't exist or is already ``source='user'``, this is a no-op.
    """
    _check_key(key)
    sess, owns = await _with_session(session)
    try:
        await sess.execute(
            text(
                "UPDATE repo_secrets "
                "SET source = 'user', purpose = NULL, updated_at = now() "
                "WHERE repo_id = :rid AND organization_id = :oid AND key = :k"
            ),
            {"rid": repo_id, "oid": organization_id, "k": key},
        )
        if owns:
            await sess.commit()
    finally:
        if owns:
            await sess.close()
