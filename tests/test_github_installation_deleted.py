# tests/test_github_installation_deleted.py
"""installation.deleted event removes the github_installations row."""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orchestrator.webhooks.github import router


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()


@pytest.fixture
def app(monkeypatch):
    from shared import config

    config.settings.github_webhook_secret = "GLOBAL"
    a = FastAPI()
    a.include_router(router, prefix="/api")
    return a


@pytest.mark.asyncio
async def test_installation_deleted_removes_row(app):
    payload = {
        "action": "deleted",
        "installation": {"id": 12345},
    }
    body = json.dumps(payload).encode()
    sig = _sign(body, "GLOBAL")

    calls = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, sql, params=None):
            calls.append((str(sql), params))
            return MagicMock()

        async def commit(self):
            return None

    with patch(
        "orchestrator.webhooks.github.async_session",
        return_value=FakeSession(),
    ), patch(
        "orchestrator.webhooks.github._secret_for_repo_full_name",
        return_value=None,
        create=True,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.post(
                "/api/webhooks/github",
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "installation",
                    "Content-Type": "application/json",
                },
                content=body,
            )

    assert resp.status_code == 200
    sqls = [c[0] for c in calls]
    assert any("DELETE FROM github_installations" in s for s in sqls)
