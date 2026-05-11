"""GitHub App OAuth install flow.

`/api/integrations/github/install` 302s to
https://github.com/apps/<slug>/installations/new?state=<signed_state>.
GitHub's flow is simpler than Slack's: the user lands on github.com,
picks repos, and gets bounced back with ?installation_id=N&state=...
We don't exchange a code — GitHub gives us the installation_id directly."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from integrations.github.oauth import router


@pytest.fixture
def app(monkeypatch):
    from shared import config
    config.settings.github_app_slug = "auto-agent"
    config.settings.slack_oauth_state_secret = "ssec"  # reused for github state
    config.settings.github_app_id = "test-app-id"
    config.settings.github_app_private_key = "test-key"

    a = FastAPI()
    a.include_router(router, prefix="/api")
    return a


@pytest.mark.asyncio
async def test_install_redirects_to_github_app_install_url(app, monkeypatch):
    from orchestrator import auth

    async def fake_admin():
        return 7

    app.dependency_overrides[auth.current_org_id_admin_dep] = fake_admin

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        resp = await c.get(
            "/api/integrations/github/install", follow_redirects=False
        )

    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith("https://github.com/apps/auto-agent/installations/new")
    assert "state=" in loc


@pytest.mark.asyncio
async def test_callback_stores_installation_id(app, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock, patch

    from integrations.slack.oauth import _sign_state

    state = _sign_state({"org_id": 42, "nonce": "abc"})

    # Create a fake response
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json = lambda: {
        "account": {"login": "acme-inc", "type": "Organization"},
    }

    # Create a fake AsyncClient context manager
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = False

    inserted = {}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, sql, params=None):
            inserted["sql"] = str(sql)
            inserted["params"] = params
            return MagicMock()

        async def commit(self):
            return None

    with patch(
        "integrations.github.oauth.httpx.AsyncClient", return_value=fake_client
    ), patch(
        "integrations.github.oauth.async_session", return_value=FakeSession()
    ), patch(
        "integrations.github.oauth._app_jwt_for_install_lookup",
        return_value="JWT",
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.get(
                "/api/integrations/github/oauth/callback",
                params={"installation_id": "12345", "state": state},
                follow_redirects=False,
            )

    assert resp.status_code == 302
    assert "INSERT INTO github_installations" in inserted["sql"]
    assert inserted["params"]["installation_id"] == 12345
    assert inserted["params"]["org_id"] == 42
    assert inserted["params"]["account_login"] == "acme-inc"
    assert inserted["params"]["account_type"] == "Organization"


@pytest.mark.asyncio
async def test_callback_rejects_tampered_state(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        resp = await c.get(
            "/api/integrations/github/oauth/callback",
            params={"installation_id": "1", "state": "bad.state"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_get_install_returns_connected(app, monkeypatch):
    from orchestrator import auth

    async def fake_admin():
        return 42

    app.dependency_overrides[auth.current_org_id_admin_dep] = fake_admin

    row = MagicMock(
        installation_id=12345, account_login="acme-inc", account_type="Organization"
    )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            return MagicMock(first=lambda: row)

    with patch(
        "integrations.github.oauth.async_session", return_value=FakeSession()
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.get("/api/integrations/github")

    body = resp.json()
    assert body == {
        "connected": True,
        "installation_id": 12345,
        "account_login": "acme-inc",
        "account_type": "Organization",
    }


@pytest.mark.asyncio
async def test_uninstall_deletes_row(app, monkeypatch):
    from orchestrator import auth

    async def fake_admin():
        return 42

    app.dependency_overrides[auth.current_org_id_admin_dep] = fake_admin

    calls = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, sql, params=None):
            calls.append(str(sql))
            return MagicMock()

        async def commit(self):
            return None

    with patch(
        "integrations.github.oauth.async_session", return_value=FakeSession()
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.post("/api/integrations/github/uninstall")

    assert resp.status_code == 200
    assert any("DELETE FROM github_installations" in s for s in calls)
