"""Slack integration — listens to a channel and forwards tasks to the orchestrator."""

from __future__ import annotations

import asyncio

import httpx
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from shared.config import settings
from shared.logging import setup_logging
from shared.types import TaskData

log = setup_logging("slack-worker")

app = AsyncApp(token=settings.slack_bot_token)

ORCHESTRATOR_URL = settings.orchestrator_url


@app.event("message")
async def handle_message(event: dict, say) -> None:
    """Forward channel messages as tasks to the orchestrator."""
    if event.get("subtype") or event.get("thread_ts"):
        return

    channel: str = event.get("channel", "")
    if channel != settings.slack_channel_id:
        return

    text: str = event.get("text", "").strip()
    if not text:
        return

    ts: str = event.get("ts", "")
    log.info(f"Received Slack message: {text[:80]}...")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/tasks",
            json={
                "title": text[:512],
                "description": text,
                "source": "slack",
                "source_id": ts,
            },
        )
        if resp.status_code == 200:
            task = TaskData.model_validate(resp.json())
            await say(f"Task #{task.id} created: _{task.title[:100]}_", thread_ts=ts)
        else:
            log.error(f"Failed to create task: {resp.status_code} {resp.text}")


async def main() -> None:
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    log.info("Slack worker starting (socket mode)...")
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
