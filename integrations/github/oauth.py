"""GitHub App OAuth install + uninstall.

The GitHub App install flow does not use OAuth's code-exchange step —
the user clicks "Install" on github.com, GitHub redirects to our
callback with ?installation_id=N&state=..., and we just trust that
installation_id (after verifying our signed `state` to prevent CSRF).

Token minting still goes through shared/github_auth.py — this module
only persists the installation_id and account_login."""
from __future__ import annotations

import logging
import secrets as pysecrets
import time
from urllib.parse import urlencode

import httpx
import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from integrations.slack.oauth import _sign_state, _verify_state
from orchestrator.auth import current_org_id_admin_dep
from shared.config import settings
from shared.database import async_session

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/integrations/github")
async def github_install_state(
    org_id: int = Depends(current_org_id_admin_dep),
):
    async with async_session() as session:
        result = await session.execute(
            text(
                """
                SELECT installation_id, account_login, account_type
                FROM github_installations
                WHERE org_id = :org_id
                """
            ),
            {"org_id": org_id},
        )
        row = result.first()
    if row is None:
        return {"connected": False}
    return {
        "connected": True,
        "installation_id": row.installation_id,
        "account_login": row.account_login,
        "account_type": row.account_type,
    }


@router.get("/integrations/github/install")
async def github_install(
    org_id: int = Depends(current_org_id_admin_dep),
):
    if not settings.github_app_slug:
        raise HTTPException(500, "GITHUB_APP_SLUG not configured")
    state = _sign_state({"org_id": org_id, "nonce": pysecrets.token_hex(8)})
    qs = urlencode({"state": state})
    return RedirectResponse(
        url=f"https://github.com/apps/{settings.github_app_slug}/installations/new?{qs}",
        status_code=302,
    )


def _app_jwt_for_install_lookup() -> str:
    """Mint a short-lived App JWT to query /app/installations/{id}.

    Same shape as shared.github_auth._build_app_jwt but isolated here so
    this module doesn't depend on github_auth's caching state."""
    if not settings.github_app_id or not settings.github_app_private_key:
        raise HTTPException(500, "GITHUB_APP_ID + private key required")
    now = int(time.time())
    raw_key = settings.github_app_private_key
    if "\\n" in raw_key and "BEGIN" in raw_key:
        raw_key = raw_key.replace("\\n", "\n")
    return pyjwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": settings.github_app_id},
        raw_key,
        algorithm="RS256",
    )


@router.get("/integrations/github/oauth/callback")
async def github_oauth_callback(
    installation_id: int = Query(...),
    state: str = Query(...),
):
    payload = _verify_state(state)
    org_id = int(payload["org_id"])

    app_jwt = _app_jwt_for_install_lookup()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://api.github.com/app/installations/{installation_id}",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if resp.status_code != 200:
        log.warning(
            "github_install_lookup_failed id=%s status=%s body=%s",
            installation_id, resp.status_code, resp.text[:300],
        )
        raise HTTPException(400, "GitHub installation lookup failed")
    body = resp.json()
    account_login = body["account"]["login"]
    account_type = body["account"].get("type", "Organization")

    async with async_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO github_installations
                    (org_id, installation_id, account_login, account_type,
                     installed_at)
                VALUES
                    (:org_id, :installation_id, :account_login, :account_type,
                     now())
                ON CONFLICT (org_id) DO UPDATE SET
                    installation_id = EXCLUDED.installation_id,
                    account_login = EXCLUDED.account_login,
                    account_type = EXCLUDED.account_type,
                    installed_at = now()
                """
            ),
            {
                "org_id": org_id,
                "installation_id": installation_id,
                "account_login": account_login,
                "account_type": account_type,
            },
        )
        await session.commit()
    log.info(
        "github_installation_saved org_id=%s installation_id=%s login=%s",
        org_id, installation_id, account_login,
    )
    return RedirectResponse(
        url="/settings/integrations/github?connected=1", status_code=302
    )


@router.post("/integrations/github/uninstall")
async def github_uninstall(
    org_id: int = Depends(current_org_id_admin_dep),
):
    async with async_session() as session:
        await session.execute(
            text("DELETE FROM github_installations WHERE org_id = :org_id"),
            {"org_id": org_id},
        )
        await session.commit()
    log.info("github_installation_deleted org_id=%s", org_id)
    return {"ok": True}
