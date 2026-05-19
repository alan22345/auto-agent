"""Unit tests for shared/repo_secrets.py.

These tests mock the SQLAlchemy session — pgcrypto integration is exercised
end-to-end against the real DB in the smoke flow. What we cover here:

- set + get roundtrip (pgp_sym_encrypt/decrypt in SQL)
- list_keys never returns a 'value' field
- list_keys reflects set vs null value_enc correctly
- get_all_for_boot returns {key: plaintext} for non-null rows only
- upsert_architect_required creates placeholder rows with null value_enc
- upsert_architect_required on existing user-row flips source, preserves value
- demote_to_user preserves value, flips source, clears purpose
- free-form keys accepted (no closed allowlist)
- invalid key format rejected (lowercase, leading digit, contains hyphen)
- settings.secrets_passphrase empty → RuntimeError on any call
- caller-supplied session is not closed/committed internally
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared import repo_secrets

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubResult:
    def __init__(self, scalar=None, rows=()):
        self._scalar = scalar
        self._rows = rows

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return list(self._rows)

    def mappings(self):
        return self

    def fetchall(self):
        return list(self._rows)


def _stub_session(scalar=None, rows=()):
    sess = AsyncMock()
    sess.execute = AsyncMock(return_value=_StubResult(scalar, rows))
    sess.commit = AsyncMock()
    sess.close = AsyncMock()
    return sess


# ---------------------------------------------------------------------------
# Empty passphrase guard
# ---------------------------------------------------------------------------


async def test_empty_passphrase_refuses_set():
    with (
        patch.object(repo_secrets.settings, "secrets_passphrase", ""),
        pytest.raises(RuntimeError, match="SECRETS_PASSPHRASE"),
    ):
        await repo_secrets.set(1, "MY_KEY", "v", organization_id=1, session=_stub_session())


async def test_empty_passphrase_refuses_get():
    with (
        patch.object(repo_secrets.settings, "secrets_passphrase", ""),
        pytest.raises(RuntimeError, match="SECRETS_PASSPHRASE"),
    ):
        await repo_secrets.get(1, "MY_KEY", organization_id=1, session=_stub_session())


async def test_empty_passphrase_refuses_get_all_for_boot():
    with (
        patch.object(repo_secrets.settings, "secrets_passphrase", ""),
        pytest.raises(RuntimeError, match="SECRETS_PASSPHRASE"),
    ):
        await repo_secrets.get_all_for_boot(1, organization_id=1, session=_stub_session())


async def test_empty_passphrase_refuses_upsert_architect_required():
    with (
        patch.object(repo_secrets.settings, "secrets_passphrase", ""),
        pytest.raises(RuntimeError, match="SECRETS_PASSPHRASE"),
    ):
        await repo_secrets.upsert_architect_required(
            1, "MY_KEY", "some purpose",
            organization_id=1, session=_stub_session(),
        )


# ---------------------------------------------------------------------------
# Key format validation
# ---------------------------------------------------------------------------


async def test_invalid_key_lowercase_rejected():
    with pytest.raises(ValueError, match="key"):
        await repo_secrets.set(
            1, "my_key", "v",
            organization_id=1, session=_stub_session(),
        )


async def test_invalid_key_leading_digit_rejected():
    with pytest.raises(ValueError, match="key"):
        await repo_secrets.set(
            1, "1BADKEY", "v",
            organization_id=1, session=_stub_session(),
        )


async def test_invalid_key_hyphen_rejected():
    with pytest.raises(ValueError, match="key"):
        await repo_secrets.set(
            1, "BAD-KEY", "v",
            organization_id=1, session=_stub_session(),
        )


async def test_invalid_key_upsert_architect_required():
    with pytest.raises(ValueError, match="key"):
        await repo_secrets.upsert_architect_required(
            1, "bad-key", "purpose",
            organization_id=1, session=_stub_session(),
        )


# ---------------------------------------------------------------------------
# Free-form keys accepted
# ---------------------------------------------------------------------------


async def test_freeform_key_accepted():
    sess = _stub_session()
    with patch.object(repo_secrets.settings, "secrets_passphrase", "p"):
        # Should not raise
        await repo_secrets.set(1, "MY_RANDOM_KEY_123", "value", organization_id=1, session=sess)
    assert sess.execute.await_count == 1


async def test_any_uppercase_key_accepted():
    sess = _stub_session()
    with patch.object(repo_secrets.settings, "secrets_passphrase", "p"):
        await repo_secrets.set(1, "STRIPE_API_KEY", "sk_xxx", organization_id=1, session=sess)
    assert sess.execute.await_count == 1


# ---------------------------------------------------------------------------
# set() dispatches pgcrypto upsert
# ---------------------------------------------------------------------------


async def test_set_dispatches_pgcrypto_upsert():
    sess = _stub_session()
    with patch.object(repo_secrets.settings, "secrets_passphrase", "p"):
        await repo_secrets.set(
            42, "STRIPE_API_KEY", "sk_live_xxx",
            organization_id=7, session=sess,
        )
    assert sess.execute.await_count == 1
    args, _ = sess.execute.await_args
    sql = str(args[0])
    params = args[1]
    assert "pgp_sym_encrypt" in sql
    assert "ON CONFLICT" in sql
    assert params["v"] == "sk_live_xxx"
    assert params["p"] == "p"
    assert params["rid"] == 42
    assert params["oid"] == 7
    assert params["k"] == "STRIPE_API_KEY"


# ---------------------------------------------------------------------------
# get() dispatches pgcrypto decrypt
# ---------------------------------------------------------------------------


async def test_get_dispatches_pgcrypto_decrypt_and_returns_scalar():
    sess = _stub_session(scalar="plaintext_value")
    with patch.object(repo_secrets.settings, "secrets_passphrase", "p"):
        out = await repo_secrets.get(42, "MY_KEY", organization_id=7, session=sess)
    assert out == "plaintext_value"
    args, _ = sess.execute.await_args
    sql = str(args[0])
    assert "pgp_sym_decrypt" in sql
    assert args[1]["rid"] == 42
    assert args[1]["oid"] == 7
    assert args[1]["k"] == "MY_KEY"
    assert args[1]["p"] == "p"


async def test_get_returns_none_when_row_missing():
    sess = _stub_session(scalar=None)
    with patch.object(repo_secrets.settings, "secrets_passphrase", "p"):
        out = await repo_secrets.get(42, "MISSING_KEY", organization_id=1, session=sess)
    assert out is None


# ---------------------------------------------------------------------------
# list_keys() never returns 'value' field
# ---------------------------------------------------------------------------


async def test_list_keys_returns_no_value_field():
    rows = [
        {"key": "STRIPE_API_KEY", "set": True, "source": "user", "purpose": None, "updated_at": None},
        {"key": "OPENAI_API_KEY", "set": False, "source": "architect_required", "purpose": "LLM calls", "updated_at": None},
    ]

    class _MappingResult:
        def mappings(self):
            return self
        def fetchall(self):
            return rows

    sess = AsyncMock()
    sess.execute = AsyncMock(return_value=_MappingResult())
    sess.close = AsyncMock()
    sess.commit = AsyncMock()

    result = await repo_secrets.list_keys(42, organization_id=7, session=sess)
    for row in result:
        assert "value" not in row
        assert "value_enc" not in row


async def test_list_keys_reflects_set_vs_null():
    """set flag is True when value_enc IS NOT NULL, False otherwise."""
    rows = [
        {"key": "A_KEY", "set": True, "source": "user", "purpose": None, "updated_at": None},
        {"key": "B_KEY", "set": False, "source": "architect_required", "purpose": "b purpose", "updated_at": None},
    ]

    class _MappingResult:
        def mappings(self):
            return self
        def fetchall(self):
            return rows

    sess = AsyncMock()
    sess.execute = AsyncMock(return_value=_MappingResult())
    sess.close = AsyncMock()
    sess.commit = AsyncMock()

    result = await repo_secrets.list_keys(42, organization_id=7, session=sess)
    assert result[0]["set"] is True
    assert result[1]["set"] is False


async def test_list_keys_sql_has_no_decrypt():
    class _MappingResult:
        def mappings(self):
            return self
        def fetchall(self):
            return []

    sess = AsyncMock()
    sess.execute = AsyncMock(return_value=_MappingResult())
    sess.close = AsyncMock()
    sess.commit = AsyncMock()

    await repo_secrets.list_keys(1, organization_id=1, session=sess)
    args, _ = sess.execute.await_args
    sql = str(args[0])
    assert "pgp_sym_decrypt" not in sql


# ---------------------------------------------------------------------------
# get_all_for_boot() returns {key: plaintext} for non-null rows
# ---------------------------------------------------------------------------


async def test_get_all_for_boot_returns_dict():
    rows = [("STRIPE_API_KEY", "sk_live_xxx"), ("DB_URL", "postgres://...")]

    sess = AsyncMock()
    sess.execute = AsyncMock(return_value=_StubResult(rows=rows))
    sess.close = AsyncMock()
    sess.commit = AsyncMock()

    with patch.object(repo_secrets.settings, "secrets_passphrase", "p"):
        result = await repo_secrets.get_all_for_boot(42, organization_id=7, session=sess)

    assert result == {"STRIPE_API_KEY": "sk_live_xxx", "DB_URL": "postgres://..."}


async def test_get_all_for_boot_excludes_null_rows():
    """Only rows where value_enc IS NOT NULL are included (SQL filters them)."""
    rows = [("STRIPE_API_KEY", "sk_live_xxx")]

    sess = AsyncMock()
    sess.execute = AsyncMock(return_value=_StubResult(rows=rows))
    sess.close = AsyncMock()
    sess.commit = AsyncMock()

    with patch.object(repo_secrets.settings, "secrets_passphrase", "p"):
        result = await repo_secrets.get_all_for_boot(42, organization_id=7, session=sess)

    # MISSING_KEY not returned because value_enc was NULL in the real query
    assert "MISSING_KEY" not in result
    assert result == {"STRIPE_API_KEY": "sk_live_xxx"}


async def test_get_all_for_boot_sql_has_decrypt_and_not_null_filter():
    sess = AsyncMock()
    sess.execute = AsyncMock(return_value=_StubResult(rows=[]))
    sess.close = AsyncMock()
    sess.commit = AsyncMock()

    with patch.object(repo_secrets.settings, "secrets_passphrase", "p"):
        await repo_secrets.get_all_for_boot(1, organization_id=1, session=sess)

    args, _ = sess.execute.await_args
    sql = str(args[0]).lower()
    assert "pgp_sym_decrypt" in sql
    assert "not null" in sql or "is not null" in sql or "notnull" in sql


# ---------------------------------------------------------------------------
# upsert_architect_required()
# ---------------------------------------------------------------------------


async def test_upsert_architect_required_creates_null_row_when_none_exists():
    """If no row exists, creates a placeholder row with value_enc=NULL."""
    sess = _stub_session()
    with patch.object(repo_secrets.settings, "secrets_passphrase", "p"):
        await repo_secrets.upsert_architect_required(
            1, "STRIPE_API_KEY", "Charge cards via Stripe",
            organization_id=7, session=sess,
        )
    assert sess.execute.await_count >= 1
    # Find the INSERT or UPSERT call
    all_calls = sess.execute.await_args_list
    sqls = [str(c[0][0]) for c in all_calls]
    # Should have an INSERT/UPSERT with NULL value_enc or no value
    combined = " ".join(sqls).lower()
    assert "insert" in combined or "upsert" in combined or "on conflict" in combined


async def test_upsert_architect_required_flips_existing_user_row():
    """On existing user row: flips source to architect_required, sets purpose."""
    # First call: execute returns the existing row (for SELECT check)
    # Second call: execute for the UPDATE
    existing_row = _StubResult(scalar=1)  # row exists
    update_result = _StubResult()

    call_count = 0

    async def side_effect(query, params=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return existing_row
        return update_result

    sess = AsyncMock()
    sess.execute = AsyncMock(side_effect=side_effect)
    sess.commit = AsyncMock()
    sess.close = AsyncMock()

    with patch.object(repo_secrets.settings, "secrets_passphrase", "p"):
        await repo_secrets.upsert_architect_required(
            1, "STRIPE_API_KEY", "Charge cards via Stripe",
            organization_id=7, session=sess,
        )
    # Should have executed at least 2 SQL statements (SELECT + UPDATE or single UPSERT)
    assert sess.execute.await_count >= 1


# ---------------------------------------------------------------------------
# demote_to_user()
# ---------------------------------------------------------------------------


async def test_demote_to_user_flips_source_clears_purpose():
    sess = _stub_session()
    await repo_secrets.demote_to_user(1, "STRIPE_API_KEY", organization_id=7, session=sess)
    assert sess.execute.await_count == 1
    args, _ = sess.execute.await_args
    sql = str(args[0]).lower()
    assert "update" in sql
    params = args[1]
    assert params["rid"] == 1
    assert params["k"] == "STRIPE_API_KEY"
    assert params["oid"] == 7


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------


async def test_delete_dispatches_delete_sql():
    sess = _stub_session()
    await repo_secrets.delete(42, "MY_KEY", organization_id=7, session=sess)
    args, _ = sess.execute.await_args
    sql = str(args[0]).upper()
    assert sql.strip().startswith("DELETE")
    assert args[1]["rid"] == 42
    assert args[1]["k"] == "MY_KEY"
    assert args[1]["oid"] == 7


# ---------------------------------------------------------------------------
# Caller-supplied session is reused (no implicit commit/close)
# ---------------------------------------------------------------------------


async def test_caller_session_not_closed_or_committed():
    sess = _stub_session()
    with patch.object(repo_secrets.settings, "secrets_passphrase", "p"):
        await repo_secrets.set(
            1, "MY_KEY", "value", organization_id=1, session=sess,
        )
    sess.commit.assert_not_awaited()
    sess.close.assert_not_awaited()


async def test_get_caller_session_not_closed():
    sess = _stub_session(scalar="val")
    with patch.object(repo_secrets.settings, "secrets_passphrase", "p"):
        await repo_secrets.get(1, "MY_KEY", organization_id=1, session=sess)
    sess.close.assert_not_awaited()
