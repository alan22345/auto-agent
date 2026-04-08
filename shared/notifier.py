"""Shared Telegram notification helper.

Uses the Telegram Bot API directly via httpx — no SDK needed.
"""

from __future__ import annotations

import logging

import httpx

from shared.config import settings

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"


def send_telegram(message: str) -> None:
    """Send a Telegram message to the configured chat.

    Silently skips if Telegram is not configured (missing token or chat_id).
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.debug("Telegram not configured, skipping message")
        return

    url = f"{TELEGRAM_API.format(token=settings.telegram_bot_token)}/sendMessage"

    try:
        resp = httpx.post(url, json={
            "chat_id": settings.telegram_chat_id,
            "text": message,
            "parse_mode": "Markdown",
        })
        if not resp.is_success:
            log.warning(f"Telegram API error: {resp.status_code} {resp.text[:200]}")
    except Exception:
        log.exception("Failed to send Telegram message")


TELEGRAM_MAX_LENGTH = 4000  # Telegram limit is 4096, leave room for formatting


async def send_telegram_async(message: str) -> None:
    """Async version of send_telegram. Splits long messages automatically."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return

    url = f"{TELEGRAM_API.format(token=settings.telegram_bot_token)}/sendMessage"

    # Split long messages into chunks
    chunks = _split_message(message, TELEGRAM_MAX_LENGTH)

    try:
        async with httpx.AsyncClient() as client:
            for chunk in chunks:
                resp = await client.post(url, json={
                    "chat_id": settings.telegram_chat_id,
                    "text": chunk,
                    "parse_mode": "Markdown",
                })
                if not resp.is_success:
                    # Retry without Markdown if parse fails
                    resp = await client.post(url, json={
                        "chat_id": settings.telegram_chat_id,
                        "text": chunk,
                    })
                    if not resp.is_success:
                        log.warning(f"Telegram API error: {resp.status_code} {resp.text[:200]}")
    except Exception:
        log.exception("Failed to send Telegram message")


def _split_message(text: str, max_len: int) -> list[str]:
    """Split a message into chunks, breaking at newlines when possible."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Find last newline within limit
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1 or split_at < max_len // 2:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks
