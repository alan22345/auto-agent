# tests/test_webhook_per_org_secret.py
"""GitHub webhook signature verification picks the secret based on the
repo's owning org. Allows two orgs to have different secrets without
cross-verification."""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orchestrator.webhooks.github import router


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(router, prefix="/api")
    return a


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()


@pytest.mark.asyncio
async def test_per_org_secret_verifies(app, monkeypatch):
    """org A's secret signs a payload for org A's repo — must verify."""
    payload = {
        "action": "completed",
        "repository": {"full_name": "acme/widgets"},
        "check_suite": {
            "conclusion": "success",
            "pull_requests": [{"html_url": "https://github.com/acme/widgets/pull/1"}],
        },
    }
    body = json.dumps(payload).encode()
    sig = _sign(body, "ORG-A-SECRET")

    async def fake_lookup_secret(*, full_name):
        # Simulates: repo "acme/widgets" → org_id=1, secret="ORG-A-SECRET".
        if full_name == "acme/widgets":
            return "ORG-A-SECRET"
        return None

    handler_called = []

    async def fake_handle_cs(p):
        handler_called.append(p)

    with patch(
        "orchestrator.webhooks.github._secret_for_repo_full_name",
        new=fake_lookup_secret, create=True,
    ), patch(
        "orchestrator.webhooks.github._handle_check_suite",
        new=fake_handle_cs,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.post(
                "/api/webhooks/github",
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "check_suite",
                    "Content-Type": "application/json",
                },
                content=body,
            )
    assert resp.status_code == 200
    assert handler_called


@pytest.mark.asyncio
async def test_per_org_secret_rejects_wrong_secret(app, monkeypatch):
    """Same payload, signed with org B's secret, fails verification
    because the repo belongs to org A."""
    payload = {"repository": {"full_name": "acme/widgets"}, "action": "completed",
               "check_suite": {}}
    body = json.dumps(payload).encode()
    bad_sig = _sign(body, "ORG-B-SECRET")

    async def fake_lookup_secret(*, full_name):
        return "ORG-A-SECRET"

    with patch(
        "orchestrator.webhooks.github._secret_for_repo_full_name",
        new=fake_lookup_secret, create=True,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.post(
                "/api/webhooks/github",
                headers={
                    "X-Hub-Signature-256": bad_sig,
                    "X-GitHub-Event": "check_suite",
                    "Content-Type": "application/json",
                },
                content=body,
            )
    assert resp.status_code == 403
