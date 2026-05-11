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


async def send_telegram_async(
    message: str,
    task_id: int | None = None,
    chat_id: str | None = None,
) -> None:
    """Async version of send_telegram. Splits long messages automatically.

    `chat_id` overrides the default destination — pass the task owner's
    chat_id when routing per-user. When omitted, falls back to the global
    ``settings.telegram_chat_id`` (admin chat).

    If `task_id` is set, the message_id of each outbound chunk is bound
    to the task via the TaskChannel seam so that a reply to this
    notification can be routed back to the task as a feedback message.
    """
    if not settings.telegram_bot_token:
        return
    target = chat_id or settings.telegram_chat_id
    if not target:
        return

    url = f"{TELEGRAM_API.format(token=settings.telegram_bot_token)}/sendMessage"
    chunks = _split_message(message, TELEGRAM_MAX_LENGTH)

    try:
        async with httpx.AsyncClient() as client:
            for chunk in chunks:
                resp = await client.post(url, json={
                    "chat_id": target,
                    "text": chunk,
                    "parse_mode": "Markdown",
                })
                if not resp.is_success:
                    resp = await client.post(url, json={
                        "chat_id": target,
                        "text": chunk,
                    })
                    if not resp.is_success:
                        log.warning(f"Telegram API error: {resp.status_code} {resp.text[:200]}")
                        continue
                if task_id is not None:
                    msg_id = resp.json().get("result", {}).get("message_id")
                    if msg_id:
                        from shared.task_channel import task_channel
                        await task_channel(task_id).bind_telegram_message(msg_id)
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
