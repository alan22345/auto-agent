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
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from integrations.slack.oauth import _sign_state  # reuse signing helper
from orchestrator.auth import current_org_id_admin_dep
from shared.config import settings

log = logging.getLogger(__name__)
router = APIRouter()


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
