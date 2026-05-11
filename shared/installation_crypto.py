"""pgcrypto encrypt/decrypt for installation tokens.

Separate from shared/secrets.py because secrets.py deals with
(user_id, org_id, key) tuples for the user_secrets table; installation
tokens live in their own tables with different keying. The encryption
primitive is identical and keyed off SECRETS_PASSPHRASE.

Rotation: change SECRETS_PASSPHRASE means every BYTEA blob in
user_secrets.value_enc, slack_installations.bot_token_enc,
slack_installations.app_token_enc, and webhook_secrets.secret_enc must
be re-encrypted in a coordinated upgrade. There is no rotation script
today (deferred until a customer asks)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from shared.config import settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def encrypt(value: str, *, session: AsyncSession) -> bytes:
    if not settings.secrets_passphrase:
        raise RuntimeError(
            "SECRETS_PASSPHRASE is not set. "
            "Set it in .env before installing integrations."
        )
    result = await session.execute(
        text("SELECT pgp_sym_encrypt(:v, :p)"),
        {"v": value, "p": settings.secrets_passphrase},
    )
    return result.scalar_one()


async def decrypt(blob: bytes, *, session: AsyncSession) -> str:
    if not settings.secrets_passphrase:
        raise RuntimeError(
            "SECRETS_PASSPHRASE is not set. "
            "Cannot decrypt installation tokens."
        )
    result = await session.execute(
        text("SELECT pgp_sym_decrypt(:b, :p)"),
        {"b": blob, "p": settings.secrets_passphrase},
    )
    return result.scalar_one()
