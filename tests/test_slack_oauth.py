"""Slack OAuth install flow.

`/api/integrations/slack/install` builds a signed state, then 302s to
slack.com/oauth/v2/authorize with the right scopes and client_id."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from integrations.slack.oauth import router


@pytest.fixture
def app():
    from shared import config

    config.settings.slack_client_id = "cid"
    config.settings.slack_client_secret = "csec"
    config.settings.slack_oauth_state_secret = "ssec"

    a = FastAPI()
    a.include_router(router)
    return a


@pytest.mark.asyncio
async def test_install_redirects_to_slack_with_state(app):
    # Bypass the admin dep — return org_id=7 directly.
    from orchestrator.auth import current_org_id_admin_dep

    async def fake_admin():
        return 7

    app.dependency_overrides[current_org_id_admin_dep] = fake_admin

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/integrations/slack/install", follow_redirects=False)

    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith("https://slack.com/oauth/v2/authorize")
    assert "client_id=cid" in loc
    assert "state=" in loc
    assert "scope=" in loc


@pytest.mark.asyncio
async def test_callback_exchanges_code_and_saves(app, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock, patch

    from integrations.slack.installation_store import PostgresInstallationStore
    from integrations.slack.oauth import _sign_state

    state = _sign_state({"org_id": 42, "nonce": "abc"})

    fake_slack_response = {
        "ok": True,
        "team": {"id": "T999", "name": "acme"},
        "bot_user_id": "UBOT",
        "access_token": "xoxb-real",
        "authed_user": {"id": "U123"},
    }

    save_mock = AsyncMock()

    async def fake_post(*args, **kwargs):
        m = MagicMock()
        m.json = lambda: fake_slack_response
        m.status_code = 200
        return m

    with patch(
        "integrations.slack.oauth.httpx.AsyncClient.post", new=fake_post
    ), patch.object(PostgresInstallationStore, "async_save", new=save_mock):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.get(
                "/api/integrations/slack/oauth/callback",
                params={"code": "abc", "state": state},
                follow_redirects=False,
            )

    assert resp.status_code == 302
    save_mock.assert_awaited_once()
    install = save_mock.await_args.args[0]
    assert install.team_id == "T999"
    assert install.bot_token == "xoxb-real"
    assert install.bot_user_id == "UBOT"


@pytest.mark.asyncio
async def test_callback_rejects_tampered_state(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        resp = await c.get(
            "/api/integrations/slack/oauth/callback",
            params={"code": "abc", "state": "tampered.bad"},
        )
    assert resp.status_code == 400
