"""Transactional email seam.

Backed by Resend's HTTPS API (https://resend.com/docs/api-reference/emails/send-email).
No SDK dependency — a single ``httpx`` POST keeps the surface area small and
easy to swap if we ever change provider.

When ``settings.resend_api_key`` is empty (typical in local dev) the helpers
log the verification URL at INFO and return — signup still completes; the
operator pulls the link out of ``docker compose logs``.
"""

from __future__ import annotations

import logging

import httpx

from shared.config import settings

log = logging.getLogger(__name__)


def _verify_url(token: str) -> str:
    base = settings.app_base_url.rstrip("/")
    return f"{base}/verify/{token}"


async def send_verification_email(to: str, token: str) -> None:
    """Dispatch the post-signup verification email.

    Idempotent at the call-site level — re-sending the same token simply
    re-delivers the link. Token rotation lives in the signup endpoint.
    """
    verify_url = _verify_url(token)
    if not settings.resend_api_key:
        # Dev fallback. Logged at INFO (not WARNING) because this is the
        # documented developer experience when no Resend key is configured.
        log.info(
            "email_verification_link_dev_fallback to=%s url=%s",
            to,
            verify_url,
        )
        return

    body = {
        "from": settings.resend_from,
        "to": [to],
        "subject": "Verify your auto-agent account",
        "text": (
            f"Welcome to auto-agent.\n\n"
            f"Click to verify your email:\n{verify_url}\n\n"
            f"This link expires in 24 hours. If you didn't sign up, "
            f"ignore this email."
        ),
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
    if resp.status_code >= 400:
        log.warning(
            "resend_send_failed status=%s body=%s to=%s",
            resp.status_code,
            resp.text[:300],
            to,
        )
        resp.raise_for_status()
