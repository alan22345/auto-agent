"""PostgresInstallationStore mirrors slack-bolt's AsyncInstallationStore
contract enough that a multi-team app can resolve bot tokens by team_id."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from integrations.slack.installation_store import PostgresInstallationStore


@pytest.mark.asyncio
async def test_save_inserts_row_with_encrypted_token():
    store = PostgresInstallationStore(org_id=42)

    # The slack-bolt Installation type has these attributes; we mock just
    # what we read.
    install = MagicMock(
        team_id="T123",
        team_name="acme",
        bot_token="xoxb-secret",
        bot_user_id="UBOTID",
        app_token=None,
        user_id="UADMIN",
    )

    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "integrations.slack.installation_store.async_session",
        return_value=session_cm,
    ), patch(
        "integrations.slack.installation_store.installation_crypto.encrypt",
        new=AsyncMock(return_value=b"CIPHER"),
    ):
        await store.async_save(install)

    call_args_list = session.execute.await_args_list
    sql_strings = [str(c.args[0]) for c in call_args_list]
    assert any("INSERT" in s and "slack_installations" in s for s in sql_strings)


@pytest.mark.asyncio
async def test_find_bot_returns_decrypted_token():
    store = PostgresInstallationStore()  # no org_id needed for lookup

    row = MagicMock()
    row.bot_token_enc = b"CIPHER"
    row.bot_user_id = "UBOTID"
    row.team_id = "T123"

    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(first=lambda: row)
    )
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "integrations.slack.installation_store.async_session",
        return_value=session_cm,
    ), patch(
        "integrations.slack.installation_store.installation_crypto.decrypt",
        new=AsyncMock(return_value="xoxb-plain"),
    ):
        bot = await store.async_find_bot(team_id="T123")

    assert bot is not None
    assert bot.bot_token == "xoxb-plain"
    assert bot.bot_user_id == "UBOTID"
    assert bot.team_id == "T123"


@pytest.mark.asyncio
async def test_find_bot_returns_none_for_unknown_team():
    store = PostgresInstallationStore()

    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(first=lambda: None))
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "integrations.slack.installation_store.async_session",
        return_value=session_cm,
    ):
        bot = await store.async_find_bot(team_id="T_UNKNOWN")
    assert bot is None


@pytest.mark.asyncio
async def test_delete_installation_by_team_id():
    store = PostgresInstallationStore()

    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "integrations.slack.installation_store.async_session",
        return_value=session_cm,
    ):
        await store.async_delete_installation(team_id="T123")

    args, _ = session.execute.call_args
    assert "DELETE FROM slack_installations" in str(args[0])


@pytest.mark.asyncio
async def test_find_by_org_id_returns_team_name():
    store = PostgresInstallationStore(org_id=42)

    row = MagicMock(team_id="T123", team_name="acme", bot_user_id="UB")
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(first=lambda: row))
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "integrations.slack.installation_store.async_session",
        return_value=session_cm,
    ):
        info = await store.find_by_org_id(42)
    assert info is not None
    assert info["team_id"] == "T123"
    assert info["team_name"] == "acme"
