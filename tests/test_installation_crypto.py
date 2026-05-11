"""installation_crypto wraps pgcrypto's pgp_sym_encrypt / pgp_sym_decrypt
exactly the same way shared/secrets.py does — it's a separate module only
so callers don't have to thread the model name + column shape through.

Mocks the session because Postgres isn't wired in test."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from shared import installation_crypto


@pytest.mark.asyncio
async def test_encrypt_calls_pgp_sym_encrypt(monkeypatch):
    monkeypatch.setattr(installation_crypto.settings, "secrets_passphrase", "p")
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one=lambda: b"CIPHER"))

    out = await installation_crypto.encrypt("plain-token", session=session)

    assert out == b"CIPHER"
    args, _ = session.execute.call_args
    sql_text = str(args[0])
    assert "pgp_sym_encrypt" in sql_text


@pytest.mark.asyncio
async def test_decrypt_calls_pgp_sym_decrypt(monkeypatch):
    monkeypatch.setattr(installation_crypto.settings, "secrets_passphrase", "p")
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one=lambda: "plain"))

    out = await installation_crypto.decrypt(b"CIPHER", session=session)

    assert out == "plain"
    args, _ = session.execute.call_args
    sql_text = str(args[0])
    assert "pgp_sym_decrypt" in sql_text


@pytest.mark.asyncio
async def test_encrypt_raises_without_passphrase(monkeypatch):
    monkeypatch.setattr(installation_crypto.settings, "secrets_passphrase", "")
    session = MagicMock()
    with pytest.raises(RuntimeError, match="SECRETS_PASSPHRASE"):
        await installation_crypto.encrypt("x", session=session)


@pytest.mark.asyncio
async def test_decrypt_raises_without_passphrase(monkeypatch):
    monkeypatch.setattr(installation_crypto.settings, "secrets_passphrase", "")
    session = MagicMock()
    with pytest.raises(RuntimeError, match="SECRETS_PASSPHRASE"):
        await installation_crypto.decrypt(b"x", session=session)
