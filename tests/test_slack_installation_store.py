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
