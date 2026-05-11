"""Slack OAuth install + callback + uninstall endpoints.

State CSRF: we sign a JSON payload {"org_id": N, "nonce": <hex>} with
HMAC-SHA256 keyed off SLACK_OAUTH_STATE_SECRET and pass it as the OAuth
`state` parameter. The callback verifies the signature and uses the
embedded org_id directly — we do NOT trust the JWT cookie at callback
time because the user might come back in a different browser window
(rare, but state-signing is cheap insurance).

Scopes requested: chat:write, im:write, im:history, users:read.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets as pysecrets
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from integrations.slack.installation_store import PostgresInstallationStore
from orchestrator.auth import current_org_id_admin_dep
from shared.config import settings

log = logging.getLogger(__name__)
router = APIRouter()


_SLACK_BOT_SCOPES = ["chat:write", "im:write", "im:history", "users:read"]


def _sign_state(payload: dict) -> str:
    if not settings.slack_oauth_state_secret:
        raise HTTPException(500, "SLACK_OAUTH_STATE_SECRET not configured")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    mac = hmac.new(
        settings.slack_oauth_state_secret.encode(),
        raw.encode(),
        hashlib.sha256,
    ).hexdigest()
    body = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    return f"{body}.{mac}"


def _verify_state(state: str) -> dict:
    if not settings.slack_oauth_state_secret:
        raise HTTPException(500, "SLACK_OAUTH_STATE_SECRET not configured")
    try:
        body_b64, mac = state.rsplit(".", 1)
        padding = "=" * (-len(body_b64) % 4)
        raw = base64.urlsafe_b64decode(body_b64 + padding).decode()
    except Exception as e:
        raise HTTPException(400, "Malformed state") from e
    expected = hmac.new(
        settings.slack_oauth_state_secret.encode(),
        raw.encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, mac):
        raise HTTPException(400, "Invalid state signature")
    return json.loads(raw)


@router.get("/api/integrations/slack/install")
async def slack_install(
    org_id: int = Depends(current_org_id_admin_dep),
):
    if not settings.slack_client_id:
        raise HTTPException(500, "SLACK_CLIENT_ID not configured")
    state = _sign_state({"org_id": org_id, "nonce": pysecrets.token_hex(8)})
    qs = urlencode(
        {
            "client_id": settings.slack_client_id,
            "scope": ",".join(_SLACK_BOT_SCOPES),
            "state": state,
        }
    )
    return RedirectResponse(url=f"https://slack.com/oauth/v2/authorize?{qs}", status_code=302)


@router.get("/api/integrations/slack/oauth/callback")
async def slack_oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
):
    payload = _verify_state(state)
    org_id = int(payload["org_id"])

    if not settings.slack_client_id or not settings.slack_client_secret:
        raise HTTPException(500, "Slack OAuth credentials not configured")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://slack.com/api/oauth.v2.access",
            data={
                "client_id": settings.slack_client_id,
                "client_secret": settings.slack_client_secret,
                "code": code,
            },
        )
    body = resp.json()
    if not body.get("ok"):
        log.warning("slack_oauth_exchange_failed body=%s", body)
        raise HTTPException(400, f"Slack OAuth failed: {body.get('error')}")

    install = SimpleNamespace(
        team_id=body["team"]["id"],
        team_name=body["team"].get("name"),
        bot_token=body["access_token"],
        bot_user_id=body["bot_user_id"],
        app_token=None,
        user_id=body.get("authed_user", {}).get("id"),
    )
    store = PostgresInstallationStore(org_id=org_id)
    await store.async_save(install)

    return RedirectResponse(url="/settings/integrations/slack?connected=1", status_code=302)


@router.get("/api/integrations/slack")
async def slack_install_state(
    org_id: int = Depends(current_org_id_admin_dep),
):
    store = PostgresInstallationStore(org_id=org_id)
    info = await store.find_by_org_id(org_id)
    if info is None:
        return {"connected": False}
    return {
        "connected": True,
        "team_id": info["team_id"],
        "team_name": info["team_name"],
    }


@router.post("/api/integrations/slack/uninstall")
async def slack_uninstall(
    org_id: int = Depends(current_org_id_admin_dep),
):
    store = PostgresInstallationStore(org_id=org_id)
    info = await store.find_by_org_id(org_id)
    if info is None:
        raise HTTPException(404, "Not installed")
    await store.async_delete_installation(team_id=info["team_id"])
    return {"ok": True}
