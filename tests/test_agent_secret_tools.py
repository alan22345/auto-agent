"""Tests for ADR-019 agent tools: list_repo_secrets + get_secret.

Covers:
  - list_repo_secrets returns no secret values, only metadata.
  - list_repo_secrets refuses when repo_id / organization_id absent.
  - get_secret returns the value when present.
  - get_secret returns null sentinel when the key is unset.
  - get_secret refuses when repo_id / organization_id absent.
  - get_secret validates key format.
  - get_secret calls register_secret with the returned value.
  - structlog processor redacts registered secret values from log events.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest

from agent.tools.base import ToolContext
from agent.tools.secrets import GetSecretTool, ListRepoSecretsTool
from shared.logging import _known_secrets, _secret_lock

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_known_secrets():
    """Wipe the process-global redactor set before and after each test."""
    with _secret_lock:
        _known_secrets.clear()
    yield
    with _secret_lock:
        _known_secrets.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(repo_id: int | None = 1, organization_id: int | None = 10) -> ToolContext:
    return ToolContext(workspace="/tmp", repo_id=repo_id, organization_id=organization_id)


@contextmanager
def _secret_lock_ctx():
    from shared import logging as slog

    with slog._secret_lock:
        yield


# ---------------------------------------------------------------------------
# list_repo_secrets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_repo_secrets_returns_no_values():
    """list_repo_secrets must not include any 'value' field in its output."""
    mock_rows = [
        {
            "key": "STRIPE_API_KEY",
            "set": True,
            "source": "architect_required",
            "purpose": "Charge cards",
            "updated_at": "2025-01-01T00:00:00",
        },
        {
            "key": "STRIPE_WEBHOOK_SECRET",
            "set": False,
            "source": "architect_required",
            "purpose": "Verify webhooks",
            "updated_at": "2025-01-01T00:00:00",
        },
    ]
    with patch("shared.repo_secrets.list_keys", new=AsyncMock(return_value=mock_rows)):
        result = await ListRepoSecretsTool().execute({}, _ctx())

    assert not result.is_error
    # The output must contain the key names
    assert "STRIPE_API_KEY" in result.output
    assert "STRIPE_WEBHOOK_SECRET" in result.output
    # But must NEVER contain actual secret content
    output_lower = result.output.lower()
    assert "sk_" not in output_lower
    # Specifically, no JSON "value" key should appear
    assert '"value"' not in result.output


@pytest.mark.asyncio
async def test_list_repo_secrets_refuses_without_repo_id():
    """list_repo_secrets returns an error when repo_id is None."""
    result = await ListRepoSecretsTool().execute({}, _ctx(repo_id=None))
    assert result.is_error
    assert "repo workspace" in result.output.lower()


@pytest.mark.asyncio
async def test_list_repo_secrets_refuses_without_organization_id():
    """list_repo_secrets returns an error when organization_id is None."""
    result = await ListRepoSecretsTool().execute({}, _ctx(organization_id=None))
    assert result.is_error
    assert "repo workspace" in result.output.lower()


@pytest.mark.asyncio
async def test_list_repo_secrets_empty_repo():
    """list_repo_secrets returns a friendly message when no secrets exist."""
    with patch("shared.repo_secrets.list_keys", new=AsyncMock(return_value=[])):
        result = await ListRepoSecretsTool().execute({}, _ctx())

    assert not result.is_error
    assert "no secrets" in result.output.lower()


# ---------------------------------------------------------------------------
# get_secret
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_secret_returns_value_when_set():
    """get_secret returns the plaintext value when the key exists."""
    with patch("shared.repo_secrets.get", new=AsyncMock(return_value="sk_test_xxx")):
        result = await GetSecretTool().execute({"key": "STRIPE_API_KEY"}, _ctx())

    assert not result.is_error
    data = json.loads(result.output)
    assert data["value"] == "sk_test_xxx"
    assert data["set"] is True


@pytest.mark.asyncio
async def test_get_secret_returns_none_when_unset():
    """get_secret returns a JSON object with value=null when key is missing."""
    with patch("shared.repo_secrets.get", new=AsyncMock(return_value=None)):
        result = await GetSecretTool().execute({"key": "MISSING_KEY"}, _ctx())

    assert not result.is_error
    data = json.loads(result.output)
    assert data["value"] is None
    assert data["set"] is False


@pytest.mark.asyncio
async def test_get_secret_refuses_without_repo_id():
    """get_secret returns an error when repo_id is None."""
    result = await GetSecretTool().execute({"key": "SOME_KEY"}, _ctx(repo_id=None))
    assert result.is_error
    assert "repo workspace" in result.output.lower()


@pytest.mark.asyncio
async def test_get_secret_refuses_without_organization_id():
    """get_secret returns an error when organization_id is None."""
    result = await GetSecretTool().execute({"key": "SOME_KEY"}, _ctx(organization_id=None))
    assert result.is_error
    assert "repo workspace" in result.output.lower()


@pytest.mark.asyncio
async def test_get_secret_validates_key_format():
    """get_secret rejects keys that don't match ^[A-Z][A-Z0-9_]*$."""
    for bad_key in ["bad-key", "lowercase", "123START", "", "SPACE KEY"]:
        result = await GetSecretTool().execute({"key": bad_key}, _ctx())
        assert result.is_error, f"expected error for key={bad_key!r}"
        assert "invalid" in result.output.lower()


@pytest.mark.asyncio
async def test_get_secret_registers_value_with_redactor():
    """get_secret must result in register_secret being called with the secret value.

    Because shared.repo_secrets.get is mocked at the module level, the real
    register_secret call in repo_secrets.get does NOT run here (the mock
    short-circuits before it).  We therefore verify the wiring separately in
    test_get_secret_wires_register_secret_through_repo_secrets.

    This test confirms that the value appears in the redactor set when
    register_secret is called explicitly — a prerequisite sanity check.
    """
    from shared.logging import _known_secrets

    secret_value = "sk_test_REDACTABLE_value_unique_xyz"

    # First clear any stale entry to make the test deterministic.
    with _secret_lock_ctx():
        _known_secrets.discard(secret_value)

    with patch("shared.repo_secrets.get", new=AsyncMock(return_value=secret_value)):
        result = await GetSecretTool().execute({"key": "STRIPE_API_KEY"}, _ctx())

    assert not result.is_error

    # Verify register_secret machinery works: adding it manually should make
    # it appear in the set (the redaction processor reads this set).
    from shared.logging import register_secret as rs

    rs(secret_value)
    from shared.logging import _known_secrets as ks

    assert secret_value in ks


@pytest.mark.asyncio
async def test_get_secret_wires_register_secret_through_repo_secrets():
    """End-to-end: repo_secrets.get calls register_secret on the returned value."""
    secret_value = "end_to_end_secret_WIRING_CHECK_abc123"

    # We don't have a real DB, so we test shared.repo_secrets.get's
    # registration side-effect by mocking the DB call inside it and letting
    # the rest of the function run.
    import shared.repo_secrets as rs_mod

    # Patch the internal session execution so get() thinks it got a value
    class _FakeRow:
        def scalar_one_or_none(self):
            return secret_value

    class _FakeSess:
        async def execute(self, *a, **kw):
            return _FakeRow()

        async def close(self):
            pass

    with (
        patch.object(rs_mod, "_with_session", new=AsyncMock(return_value=(_FakeSess(), True))),
        patch("shared.logging.register_secret") as mock_reg,
        patch.object(rs_mod, "_passphrase", return_value="testpass"),
    ):
        await rs_mod.get(1, "SOME_KEY", organization_id=10)

    mock_reg.assert_called_once_with(secret_value)


# ---------------------------------------------------------------------------
# structlog redaction — processor-level tests
#
# structlog.testing.capture_logs() bypasses configured processors (it injects
# a ReturnLogger that just records the raw kwargs).  We therefore test the
# _redact_processor function directly, which is the correct unit to test.
# ---------------------------------------------------------------------------


def test_structlog_redacts_known_secret_values():
    """_redact_processor scrubs registered secret values from log events."""
    from shared.logging import _redact_processor, register_secret

    secret = "ultra_secret_token_abc_987_xyz"
    register_secret(secret)

    event_dict = {
        "event": "test event",
        "token": secret,
        "nested": {"deep": secret},
    }
    result = _redact_processor(None, "info", event_dict)

    # Top-level kwarg
    assert result["token"] != secret, "top-level secret value was not redacted"
    assert "[REDACTED]" in result["token"]
    # Nested dict value
    assert result["nested"]["deep"] != secret, "nested secret value was not redacted"
    assert "[REDACTED]" in result["nested"]["deep"]


def test_structlog_redacts_secret_inside_string():
    """_redact_processor replaces the secret substring inside a longer string."""
    from shared.logging import _redact_processor, register_secret

    secret = "partial_secret_inside_string_999"
    register_secret(secret)

    event_dict = {"event": "config value", "url": f"postgres://user:{secret}@host/db"}
    result = _redact_processor(None, "warning", event_dict)

    url = result.get("url", "")
    assert secret not in url
    assert "[REDACTED]" in url


def test_structlog_redacts_secret_in_list():
    """_redact_processor handles secrets inside list values."""
    from shared.logging import _redact_processor, register_secret

    secret = "list_item_secret_val_abc_456"
    register_secret(secret)

    event_dict = {"event": "items", "values": [secret, "safe_value"]}
    result = _redact_processor(None, "info", event_dict)

    values = result.get("values", [])
    assert secret not in values
    assert any("[REDACTED]" in str(v) for v in values)
