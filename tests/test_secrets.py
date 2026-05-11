"""Unit tests for shared/secrets.py.

These tests mock the SQLAlchemy session — pgcrypto integration is exercised
end-to-end against the real DB in the smoke flow. What we cover here:

- key allowlist enforcement (UnknownSecretKey raised for unlisted keys)
- empty-passphrase refusal (boot-time safety)
- correct SQL + parameters dispatched for set/get/delete/list_keys
- list_keys returns names only (decryption is never invoked)
- org_id is plumbed through every operation (Phase 2 — per-(user, org) key)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared import secrets


class _StubResult:
    def __init__(self, scalar=None, rows=()):
        self._scalar = scalar
        self._rows = rows

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return list(self._rows)


def _stub_session(scalar=None, rows=()):
    sess = AsyncMock()
    sess.execute = AsyncMock(return_value=_StubResult(scalar, rows))
    sess.commit = AsyncMock()
    sess.close = AsyncMock()
    return sess


# ---------------------------------------------------------------------------
# Allowlist enforcement
# ---------------------------------------------------------------------------


async def test_set_unknown_key_raises():
    with pytest.raises(secrets.UnknownSecretKey):
        await secrets.set(1, "not_a_real_key", "x", org_id=1, session=_stub_session())


async def test_get_unknown_key_raises():
    with pytest.raises(secrets.UnknownSecretKey):
        await secrets.get(1, "not_a_real_key", org_id=1, session=_stub_session())


async def test_delete_unknown_key_raises():
    with pytest.raises(secrets.UnknownSecretKey):
        await secrets.delete(1, "not_a_real_key", org_id=1, session=_stub_session())


# ---------------------------------------------------------------------------
# Empty passphrase
# ---------------------------------------------------------------------------


async def test_empty_passphrase_refuses_writes():
    with (
        patch.object(secrets.settings, "secrets_passphrase", ""),
        pytest.raises(RuntimeError, match="SECRETS_PASSPHRASE"),
    ):
        await secrets.set(1, "github_pat", "x", org_id=1, session=_stub_session())


async def test_empty_passphrase_refuses_reads():
    with (
        patch.object(secrets.settings, "secrets_passphrase", ""),
        pytest.raises(RuntimeError, match="SECRETS_PASSPHRASE"),
    ):
        await secrets.get(1, "github_pat", org_id=1, session=_stub_session())


# ---------------------------------------------------------------------------
# SQL dispatch
# ---------------------------------------------------------------------------


async def test_set_dispatches_pgcrypto_upsert():
    sess = _stub_session()
    with patch.object(secrets.settings, "secrets_passphrase", "p"):
        await secrets.set(42, "github_pat", "ghp_xxx", org_id=7, session=sess)
    assert sess.execute.await_count == 1
    args, _ = sess.execute.await_args
    sql = str(args[0])
    params = args[1]
    assert "pgp_sym_encrypt" in sql
    assert "ON CONFLICT" in sql
    assert "organization_id" in sql
    assert params == {
        "uid": 42, "oid": 7, "k": "github_pat", "v": "ghp_xxx", "p": "p",
    }


async def test_get_dispatches_pgcrypto_decrypt_and_returns_scalar():
    sess = _stub_session(scalar="plaintext")
    with patch.object(secrets.settings, "secrets_passphrase", "p"):
        out = await secrets.get(42, "github_pat", org_id=7, session=sess)
    assert out == "plaintext"
    args, _ = sess.execute.await_args
    sql = str(args[0])
    assert "pgp_sym_decrypt" in sql
    assert "organization_id" in sql
    assert args[1] == {"uid": 42, "oid": 7, "k": "github_pat", "p": "p"}


async def test_get_returns_none_when_row_missing():
    sess = _stub_session(scalar=None)
    with patch.object(secrets.settings, "secrets_passphrase", "p"):
        out = await secrets.get(42, "github_pat", org_id=1, session=sess)
    assert out is None


async def test_delete_runs_delete_sql():
    sess = _stub_session()
    await secrets.delete(42, "github_pat", org_id=7, session=sess)
    args, _ = sess.execute.await_args
    sql = str(args[0])
    assert sql.strip().upper().startswith("DELETE")
    assert "organization_id" in sql
    assert args[1] == {"uid": 42, "oid": 7, "k": "github_pat"}


async def test_list_keys_returns_names_only_no_decryption():
    sess = _stub_session(rows=[("github_pat",), ("anthropic_api_key",)])
    out = await secrets.list_keys(42, org_id=7, session=sess)
    assert out == ["github_pat", "anthropic_api_key"]
    args, _ = sess.execute.await_args
    sql = str(args[0])
    # No decryption happens during a key listing.
    assert "pgp_sym_decrypt" not in sql
    assert "value_enc" not in sql
    assert "organization_id" in sql


# ---------------------------------------------------------------------------
# Caller-supplied session is reused (no implicit commit/close)
# ---------------------------------------------------------------------------


async def test_caller_session_not_closed_or_committed():
    sess = _stub_session()
    with patch.object(secrets.settings, "secrets_passphrase", "p"):
        await secrets.set(1, "github_pat", "x", org_id=1, session=sess)
    sess.commit.assert_not_awaited()
    sess.close.assert_not_awaited()
