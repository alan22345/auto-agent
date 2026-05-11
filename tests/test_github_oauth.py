"""GitHub App OAuth install flow.

`/api/integrations/github/install` 302s to
https://github.com/apps/<slug>/installations/new?state=<signed_state>.
GitHub's flow is simpler than Slack's: the user lands on github.com,
picks repos, and gets bounced back with ?installation_id=N&state=...
We don't exchange a code — GitHub gives us the installation_id directly."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from integrations.github.oauth import router


@pytest.fixture
def app(monkeypatch):
    from shared import config
    config.settings.github_app_slug = "auto-agent"
    config.settings.slack_oauth_state_secret = "ssec"  # reused for github state

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
