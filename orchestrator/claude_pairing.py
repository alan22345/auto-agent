"""Direct OAuth (PKCE) pairing for Claude Code.

The previous design drove `claude setup-token` / `claude auth login` through
a pseudo-terminal, but the CLI's OAuth flow either (a) ignores stdin entirely
(the v2.1 `auth login` subcommand expects a server-side bridge that doesn't
exist when running headless), or (b) requested too narrow a scope. Both
modes left ``~/.claude/.credentials.json`` unwritten.

Instead, we replicate the CLI's OAuth flow in plain HTTP:

1. ``start_pairing(user_id)`` generates a PKCE pair + ``state`` and returns
   the authorize URL the user opens in their browser.
2. The browser redirects to ``platform.claude.com/oauth/code/callback`` which
   displays the one-time ``code`` for the user to copy back.
3. ``complete_pairing(pairing_id, code)`` POSTs to the Anthropic token
   endpoint, writes ``~/.claude/.credentials.json`` in the same format the
   official CLI produces, and marks the session done.

The endpoints + client_id were lifted from the bundled CLI binary
(`claude.exe` strings) so they match what the official tool does.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from orchestrator.claude_auth import ensure_vault_dir

log = logging.getLogger(__name__)

PAIRING_TTL_SECONDS = 600

# Lifted from the CLI binary (grep `oauth` in `claude.exe`):
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
OAUTH_SCOPES = (
    "org:create_api_key user:profile user:inference user:sessions:claude_code "
    "user:mcp_servers user:file_upload"
)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


@dataclass
class PairingResult:
    success: bool
    stderr: str
    exit_code: int


@dataclass
class PairingSession:
    user_id: int
    home_dir: str
    pairing_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    code_verifier: str = field(default_factory=str)
    state: str = field(default_factory=str)
    authorize_url: str = field(default_factory=str)


_registry: dict[str, PairingSession] = {}


def _build_authorize_url(challenge: str, state: str) -> str:
    params = {
        "code": "true",
        "client_id": OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


async def start_pairing(user_id: int) -> PairingSession:
    """Create a pairing session and return it. The caller should hand the
    user ``session.authorize_url`` to open in their browser."""
    home_dir = ensure_vault_dir(user_id)
    _gc_expired()
    verifier, challenge = _make_pkce()
    state = _b64url(secrets.token_bytes(24))
    session = PairingSession(
        user_id=user_id,
        home_dir=home_dir,
        code_verifier=verifier,
        state=state,
        authorize_url=_build_authorize_url(challenge, state),
    )
    _registry[session.pairing_id] = session
    return session


def get_pairing(pairing_id: str) -> PairingSession | None:
    _gc_expired()
    return _registry.get(pairing_id)


async def complete_pairing(
    pairing_id: str, code_input: str
) -> PairingResult:
    """Exchange the user-pasted ``code#state`` (or just ``code``) for tokens
    and persist them in the user's vault. Returns a PairingResult; on
    success the registry entry is dropped and ``.credentials.json`` exists."""
    sess = _registry.get(pairing_id)
    if sess is None:
        return PairingResult(False, "pairing session not found", -1)

    # The platform.claude.com callback page typically shows ``code#state``;
    # accept either ``code`` alone or ``code#state``.
    raw = code_input.strip()
    code = raw.split("#", 1)[0]

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "code_verifier": sess.code_verifier,
        "state": sess.state,
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                OAUTH_TOKEN_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as e:
        log.exception("token exchange transport error")
        return PairingResult(False, f"token exchange failed: {e}", -1)

    if resp.status_code != 200:
        body = resp.text[:500]
        log.warning("token exchange status=%s body=%s", resp.status_code, body)
        return PairingResult(
            False,
            f"token exchange returned {resp.status_code}: {body}",
            resp.status_code,
        )

    tokens = resp.json()
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not access or not refresh:
        return PairingResult(False, "token response missing tokens", -1)

    expires_in = int(tokens.get("expires_in", 0))
    expires_at = int(time.time() * 1000) + expires_in * 1000

    scopes_raw = tokens.get("scope") or OAUTH_SCOPES
    scopes = sorted(scopes_raw.split())

    creds = {
        "claudeAiOauth": {
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": expires_at,
            "scopes": scopes,
            "subscriptionType": tokens.get("subscription_type") or "unknown",
        }
    }

    claude_dir = os.path.join(sess.home_dir, ".claude")
    os.makedirs(claude_dir, mode=0o700, exist_ok=True)
    cred_path = os.path.join(claude_dir, ".credentials.json")
    tmp_path = cred_path + ".tmp"
    await asyncio.get_event_loop().run_in_executor(
        None, _atomic_write_creds, tmp_path, cred_path, creds
    )

    _registry.pop(pairing_id, None)
    return PairingResult(True, "", 0)


def _atomic_write_creds(tmp_path: str, final_path: str, creds: dict) -> None:
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(creds, f)
    except Exception:
        try:
            os.unlink(tmp_path)
        finally:
            raise
    os.replace(tmp_path, final_path)
    os.chmod(final_path, 0o600)


def _gc_expired() -> None:
    now = time.time()
    for pid, sess in list(_registry.items()):
        if now - sess.created_at > PAIRING_TTL_SECONDS:
            _registry.pop(pid, None)
