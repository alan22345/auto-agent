"""Redis Streams helpers for event bus communication."""

from __future__ import annotations

import redis.asyncio as aioredis

from shared.config import settings

STREAM_KEY = "autoagent:events"
GROUP_NAME = "autoagent"


async def get_redis() -> aioredis.Redis:
    return aioredis.from_url(settings.redis_url, decode_responses=False)


ALL_GROUPS = ["orchestrator", "claude-runner", "telegram", "web-ui"]


async def ensure_stream_group(r: aioredis.Redis, stream: str = STREAM_KEY, group: str | None = None) -> None:
    """Create consumer group(s) if they don't exist.

    If group is None, creates all known groups so every service gets every event.
    """
    groups = [group] if group else ALL_GROUPS
    for g in groups:
        try:
            await r.xgroup_create(stream, g, id="0", mkstream=True)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise


async def publish_event(r: aioredis.Redis, event_data: dict[str, str], stream: str = STREAM_KEY) -> str:
    """Publish an event to the stream. Returns the message ID."""
    return await r.xadd(stream, event_data)


async def read_events(
    r: aioredis.Redis,
    consumer: str,
    count: int = 10,
    block: int = 5000,
    stream: str = STREAM_KEY,
    group: str | None = None,
) -> list[tuple[str, dict]]:
    """Read events from the consumer group. Returns list of (message_id, data).

    Group defaults to the consumer name so each service gets every event.
    """
    group = group or consumer
    results = await r.xreadgroup(group, consumer, {stream: ">"}, count=count, block=block)
    messages = []
    for _stream_name, entries in results:
        for msg_id, data in entries:
            messages.append((msg_id, data))
    return messages


async def ack_event(r: aioredis.Redis, msg_id: str, stream: str = STREAM_KEY, group: str | None = None, consumer: str | None = None) -> None:
    """Acknowledge a processed message. Group defaults to consumer name."""
    g = group or consumer or GROUP_NAME
    await r.xack(stream, g, msg_id)
