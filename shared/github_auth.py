"""GitHub authentication seam.

Returns a token for GitHub API + ``git`` operations. Four providers, picked
in order:

1. **Per-user PAT** — when ``user_id`` is supplied AND that user has stored
   a ``github_pat`` via ``shared.secrets``. The PAT runs as the human
   teammate, so PRs/commits surface under their identity.
2. **Per-org GitHub App installation token** (Phase 3 NEW) — when
   ``organization_id`` is supplied AND ``github_installations[org_id]``
   exists. Minted against that org's installation_id.
3. **Env-level GitHub App installation token** — when ``github_app_id``,
   ``github_app_private_key``, and ``github_app_installation_id`` are all
   configured. Short-lived (~1h), minted on demand and cached until 5 min
   before expiry. Commits and PRs surface as ``auto-agent[bot]``.
4. **Personal access token** (``github_token`` env var) — legacy fallback.

Every call site that previously read ``settings.github_token`` should now
call ``await get_github_token(user_id=task.created_by_user_id)``. Org/process
paths (pollers) call ``get_github_token()`` with no user_id and pick up the
App or env-PAT path. The fallback chain is byte-equivalent to the old
behaviour so paths without per-user PATs still work unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC

import httpx
import jwt

from shared.config import settings

log = logging.getLogger(__name__)

# Logged-once memo so we don't spam INFO on every token mint. Keyed by mode
# string; values are True once seen.
_logged_modes: set[str] = set()


def _log_mode_once(mode: str) -> None:
    if mode not in _logged_modes:
        _logged_modes.add(mode)
        log.info("github_auth_mode mode=%s", mode)


# Mint a fresh installation token when fewer than 5 min remain on the
# cached one. Installation tokens last ~1h, so this gives every caller a
# valid token without minting on every single API call.
_REFRESH_MARGIN_SECONDS = 300

_lock = asyncio.Lock()


@dataclass
class _CachedToken:
    value: str
    expires_at: float  # epoch seconds


# Keyed by installation_id (int). One global dict instead of a single slot
# so each org's installation gets its own cache entry.
_cached: dict[int, _CachedToken] = {}


def _app_configured() -> bool:
    return bool(
        settings.github_app_id
        and settings.github_app_private_key
        and settings.github_app_installation_id
    )


def _decode_private_key(raw: str) -> str:
    """`.env` files often store multi-line PEM keys with literal ``\\n``.
    Restore real newlines so PyJWT / cryptography can parse the key."""
    if "\\n" in raw and "BEGIN" in raw:
        return raw.replace("\\n", "\n")
    return raw


def _build_app_jwt() -> str:
    now = int(time.time())
    payload = {
        # Allow 60s clock skew on the issued-at — GitHub's docs explicitly
        # recommend this margin to avoid "iat in the future" rejections.
        "iat": now - 60,
        "exp": now + 540,  # max allowed is 10 min
        "iss": settings.github_app_id,
    }
    private_key = _decode_private_key(settings.github_app_private_key)
    return jwt.encode(payload, private_key, algorithm="RS256")


async def _mint_installation_token_for(installation_id: int) -> _CachedToken:
    """Mint a fresh installation token against the given installation_id."""
    app_jwt = _build_app_jwt()
    url = (
        f"https://api.github.com/app/installations/"
        f"{installation_id}/access_tokens"
    )
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if resp.status_code != 201:
        raise RuntimeError(
            f"GitHub App token mint failed for installation {installation_id}: "
            f"{resp.status_code} {resp.text[:300]}"
        )
    body = resp.json()
    token = body["token"]
    # Parse "expires_at": "2024-01-01T00:00:00Z"
    from datetime import datetime

    expires_at = (
        datetime.strptime(body["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=UTC)
        .timestamp()
    )
    return _CachedToken(value=token, expires_at=expires_at)


async def _mint_installation_token() -> _CachedToken:
    """Thin shim — mints against the env-level installation_id."""
    return await _mint_installation_token_for(
        int(settings.github_app_installation_id)
    )


async def _installation_id_for_org(*, org_id: int) -> int | None:
    """Look up the per-org GitHub App installation_id, if any.

    Returns the int installation_id (which we pass to
    /app/installations/{id}/access_tokens), or None if this org hasn't
    installed the App yet — in which case the caller falls back to the
    env-level installation_id."""
    from sqlalchemy import text

    from shared.database import async_session

    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT installation_id FROM github_installations "
                "WHERE org_id = :org_id"
            ),
            {"org_id": org_id},
        )
        row = result.first()
        return row.installation_id if row else None


async def get_github_token(
    user_id: int | None = None,
    *,
    organization_id: int | None = None,
) -> str:
    """Return a usable GitHub token.

    Async because the App path performs an HTTP call (and the per-user
    lookup hits Postgres). Returns ``""`` if nothing is configured —
    callers should still guard on truthiness.

    Resolution order (highest priority first):
        1. ``user_secrets[user_id, organization_id, "github_pat"]`` if
           both ``user_id`` AND ``organization_id`` are set.
        2. Per-org github_installations[organization_id] — mint against
           that installation_id (Phase 3 NEW)
        3. Env-level GitHub App installation token if app credentials are
           configured.
        4. Legacy env-var PAT (``settings.github_token``).

    Phase 2: ``organization_id`` is required to look up a per-user PAT
    because secrets are now keyed by (user, org). Passing only ``user_id``
    skips the per-user lookup and falls through to the App/env path —
    used by pollers that have no tenant context.
    """
    # 1. Per-user PAT
    if user_id is not None and organization_id is not None:
        try:
            # Local import — ``shared.secrets`` imports config + database,
            # which would form an import cycle if hoisted to module top.
            from shared import secrets as _secrets

            pat = await _secrets.get(
                user_id, "github_pat", org_id=organization_id,
            )
        except Exception as e:
            log.warning(
                "github_user_pat_lookup_failed user_id=%s org_id=%s err=%s",
                user_id, organization_id, e,
            )
            pat = None
        if pat:
            _log_mode_once("user_pat")
            return pat

    # 2. Per-org GitHub App installation (NEW in Phase 3)
    if organization_id is not None:
        try:
            install_id = await _installation_id_for_org(org_id=organization_id)
        except Exception as e:
            log.warning(
                "github_org_install_lookup_failed org_id=%s err=%s",
                organization_id, e,
            )
            install_id = None
        if install_id is not None:
            now = time.time()
            cached = _cached.get(install_id)
            if cached is not None and cached.expires_at - now > _REFRESH_MARGIN_SECONDS:
                _log_mode_once("org_app")
                return cached.value
            async with _lock:
                cached = _cached.get(install_id)
                now = time.time()
                if cached is not None and cached.expires_at - now > _REFRESH_MARGIN_SECONDS:
                    _log_mode_once("org_app")
                    return cached.value
                try:
                    cached = await _mint_installation_token_for(install_id)
                    _cached[install_id] = cached
                    _log_mode_once("org_app")
                    return cached.value
                except Exception as e:
                    log.warning(
                        "github_org_app_mint_failed_falling_back_to_env install_id=%s err=%s",
                        install_id, e,
                    )

    # 3. Env-level GitHub App
    if not _app_configured():
        _log_mode_once("env_pat")
        return settings.github_token or ""

    now = time.time()
    env_install_id = int(settings.github_app_installation_id)
    cached = _cached.get(env_install_id)
    if cached is not None and cached.expires_at - now > _REFRESH_MARGIN_SECONDS:
        _log_mode_once("app")
        return cached.value

    async with _lock:
        cached = _cached.get(env_install_id)
        now = time.time()
        if cached is not None and cached.expires_at - now > _REFRESH_MARGIN_SECONDS:
            _log_mode_once("app")
            return cached.value
        try:
            cached = await _mint_installation_token_for(env_install_id)
            _cached[env_install_id] = cached
            _log_mode_once("app")
            return cached.value
        except Exception as e:
            log.warning(
                "github_app_mint_failed_falling_back_to_pat error=%s", e,
            )
            _log_mode_once("env_pat_after_app_failure")
            return settings.github_token or ""


def reset_cache() -> None:
    """Test hook — drop all cached tokens AND the logged-mode memo."""
    global _cached
    _cached = {}
    _logged_modes.clear()
