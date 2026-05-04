"""Per-task Redis state seam.

ADR-007 deepened the broadcast-event publisher into a Publisher seam, but
left the *per-task* state side untouched. The same Protocol + RedisXxx +
InMemoryXxx + module-level facade shape applies to the four keys that key
off ``task_id``: the guidance list, heartbeat key, streaming pub/sub
channel, and the Telegram message → task reply-binding key. ADR-010
records that decision; this module is its implementation.

This module is the single owner of:

* the four key strings (``task:{id}:guidance``, ``task:{id}:heartbeat``,
  ``task:{id}:stream``, ``telegram:msg:{id}``);
* the heartbeat TTL (15 minutes) and telegram-binding TTL (7 days);
* the JSON wire format for streamed events (consumed by
  ``web/main.py``'s WebSocket relay);
* the connection lifecycle — a single long-lived ``redis.asyncio.Redis``
  client per process, mirroring ``RedisStreamPublisher``.

Production wires :class:`RedisTaskChannelFactory` in ``run.py``'s
lifespan; tests install :class:`InMemoryTaskChannelFactory` via an
autouse fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from shared.logging import setup_logging

log = setup_logging("shared.task_channel")


# ---------------------------------------------------------------------------
# Key scheme — referenced from exactly one module (this one).
# ---------------------------------------------------------------------------


def _guidance_key(task_id: int) -> str:
    return f"task:{task_id}:guidance"


def _heartbeat_key(task_id: int) -> str:
    return f"task:{task_id}:heartbeat"


def _stream_channel(task_id: int) -> str:
    return f"task:{task_id}:stream"


def _telegram_binding_key(message_id: int) -> str:
    return f"telegram:msg:{message_id}"


HEARTBEAT_TTL_SECONDS = 900  # 15 minutes
TELEGRAM_BINDING_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days

# Wildcard pattern for the consumer side that subscribes to every task's
# stream at once (web/main.py's WebSocket relay). Lives here so the key
# shape has exactly one home — even the wildcard subscriber doesn't
# hard-code the prefix.
TASK_STREAM_PATTERN = "task:*:stream"


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class TaskChannel(Protocol):
    """Per-task Redis state surface. One instance per ``task_id``.

    Implementations own the key scheme, the JSON encoding, and the
    connection lifecycle. Callers reach a channel via
    :func:`task_channel` and never see Redis directly.
    """

    task_id: int

    async def push_guidance(self, message: str) -> None: ...
    async def pop_guidance(self) -> str | None: ...
    async def heartbeat(self) -> None: ...
    async def is_alive(self) -> bool: ...
    async def stream_tool_call(
        self,
        tool: str,
        args_preview: str,
        result_preview: str,
        turn: int,
    ) -> None: ...
    async def stream_thinking(self, text: str, turn: int) -> None: ...
    async def bind_telegram_message(self, message_id: int) -> None: ...


class TaskChannelFactory(Protocol):
    """Owns the shared resource (Redis client / in-memory dicts) and
    returns per-task :class:`TaskChannel` handles.

    The reverse-lookup ``task_id_for_telegram_message`` lives on the
    factory rather than on a per-task channel because at lookup time the
    caller has no ``task_id`` — the ``message_id`` *is* the lookup key.
    """

    def for_task(self, task_id: int) -> TaskChannel: ...
    async def task_id_for_telegram_message(self, message_id: int) -> int | None: ...
    async def aclose(self) -> None: ...


# ---------------------------------------------------------------------------
# Redis adapter
# ---------------------------------------------------------------------------


class RedisTaskChannel:
    """Production per-task channel — issues commands against the shared
    long-lived client owned by :class:`RedisTaskChannelFactory`."""

    def __init__(self, task_id: int, factory: RedisTaskChannelFactory) -> None:
        self.task_id = task_id
        self._factory = factory

    async def push_guidance(self, message: str) -> None:
        client = await self._factory._get_client()
        await client.rpush(_guidance_key(self.task_id), message)

    async def pop_guidance(self) -> str | None:
        try:
            client = await self._factory._get_client()
            raw = await client.lpop(_guidance_key(self.task_id))
        except Exception:
            log.exception("pop_guidance failed", task_id=self.task_id)
            return None
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    async def heartbeat(self) -> None:
        try:
            client = await self._factory._get_client()
            await client.set(
                _heartbeat_key(self.task_id), "1", ex=HEARTBEAT_TTL_SECONDS
            )
        except Exception:
            log.exception("heartbeat failed", task_id=self.task_id)

    async def is_alive(self) -> bool:
        try:
            client = await self._factory._get_client()
            return bool(await client.exists(_heartbeat_key(self.task_id)))
        except Exception:
            log.exception("is_alive failed", task_id=self.task_id)
            return False

    async def stream_tool_call(
        self,
        tool: str,
        args_preview: str,
        result_preview: str,
        turn: int,
    ) -> None:
        await self._publish_stream(
            "tool",
            {
                "tool": tool,
                "args_preview": args_preview,
                "result_preview": result_preview,
                "turn": turn,
            },
        )

    async def stream_thinking(self, text: str, turn: int) -> None:
        await self._publish_stream("thinking", {"text": text, "turn": turn})

    async def _publish_stream(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            client = await self._factory._get_client()
            await client.publish(
                _stream_channel(self.task_id),
                json.dumps({"type": event_type, **payload}),
            )
        except Exception:
            log.exception(
                "stream publish failed",
                task_id=self.task_id,
                event_type=event_type,
            )

    async def bind_telegram_message(self, message_id: int) -> None:
        try:
            client = await self._factory._get_client()
            await client.set(
                _telegram_binding_key(message_id),
                str(self.task_id),
                ex=TELEGRAM_BINDING_TTL_SECONDS,
            )
        except Exception:
            log.exception(
                "bind_telegram_message failed",
                task_id=self.task_id,
                message_id=message_id,
            )


class RedisTaskChannelFactory:
    """Production factory — owns one lazy long-lived Redis client.

    The lifecycle requirement is identical to
    :class:`shared.events.RedisStreamPublisher`: ``aioredis.from_url``
    pools connections internally, so a single client suffices for the
    process lifetime. Per-call open/close (the dance every site
    previously ran) is exactly what this seam removes.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> Any:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    import redis.asyncio as aioredis

                    self._client = aioredis.from_url(
                        self._url, decode_responses=False
                    )
        return self._client

    def for_task(self, task_id: int) -> RedisTaskChannel:
        return RedisTaskChannel(task_id, self)

    async def task_id_for_telegram_message(self, message_id: int) -> int | None:
        try:
            client = await self._get_client()
            raw = await client.get(_telegram_binding_key(message_id))
        except Exception:
            log.exception(
                "task_id_for_telegram_message failed", message_id=message_id
            )
            return None
        if raw is None:
            return None
        decoded = raw.decode() if isinstance(raw, bytes) else str(raw)
        try:
            return int(decoded)
        except ValueError:
            return None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ---------------------------------------------------------------------------
# In-memory adapter — pays for itself via tests/test_task_messages.py.
# ---------------------------------------------------------------------------


class InMemoryTaskChannel:
    """Test per-task channel that delegates to its factory's storage."""

    def __init__(self, task_id: int, factory: InMemoryTaskChannelFactory) -> None:
        self.task_id = task_id
        self._factory = factory

    async def push_guidance(self, message: str) -> None:
        self._factory.guidance.setdefault(self.task_id, []).append(message)

    async def pop_guidance(self) -> str | None:
        queue = self._factory.guidance.get(self.task_id)
        if not queue:
            return None
        return queue.pop(0)

    async def heartbeat(self) -> None:
        self._factory.heartbeats[self.task_id] = datetime.now(UTC)

    async def is_alive(self) -> bool:
        ts = self._factory.heartbeats.get(self.task_id)
        if ts is None:
            return False
        return datetime.now(UTC) - ts <= timedelta(seconds=HEARTBEAT_TTL_SECONDS)

    async def stream_tool_call(
        self,
        tool: str,
        args_preview: str,
        result_preview: str,
        turn: int,
    ) -> None:
        self._factory.streams.append(
            (
                self.task_id,
                "tool",
                {
                    "tool": tool,
                    "args_preview": args_preview,
                    "result_preview": result_preview,
                    "turn": turn,
                },
            )
        )

    async def stream_thinking(self, text: str, turn: int) -> None:
        self._factory.streams.append(
            (self.task_id, "thinking", {"text": text, "turn": turn})
        )

    async def bind_telegram_message(self, message_id: int) -> None:
        self._factory.telegram_bindings[message_id] = self.task_id


class InMemoryTaskChannelFactory:
    """Test factory — captures pushes/heartbeats/streams in inspectable
    structures. Tests assert directly against these attributes."""

    def __init__(self) -> None:
        self.guidance: dict[int, list[str]] = {}
        self.heartbeats: dict[int, datetime] = {}
        self.streams: list[tuple[int, str, dict[str, Any]]] = []
        self.telegram_bindings: dict[int, int] = {}

    def for_task(self, task_id: int) -> InMemoryTaskChannel:
        return InMemoryTaskChannel(task_id, self)

    async def task_id_for_telegram_message(self, message_id: int) -> int | None:
        return self.telegram_bindings.get(message_id)

    async def aclose(self) -> None:
        self.guidance.clear()
        self.heartbeats.clear()
        self.streams.clear()
        self.telegram_bindings.clear()


# ---------------------------------------------------------------------------
# Module-level facade — the single import every call site uses.
# ---------------------------------------------------------------------------


_factory: TaskChannelFactory | None = None


def set_task_channel_factory(factory: TaskChannelFactory) -> None:
    """Register the active factory. Called once at process start
    (production) and per-test (in fixtures).

    Does NOT aclose the previous factory — if the caller is replacing a
    live one, it owns the cleanup. Production wires a single
    :class:`RedisTaskChannelFactory` in ``run.py``'s lifespan and acloses
    it on shutdown; tests use a per-test fixture that restores the
    previous reference without calling this again.
    """
    global _factory
    _factory = factory


def get_task_channel_factory() -> TaskChannelFactory:
    """Return the active factory. Raises if none is registered — every
    process must wire one in before using the seam."""
    if _factory is None:
        raise RuntimeError(
            "No TaskChannelFactory registered. Call set_task_channel_factory(...) "
            "at startup, or install an InMemoryTaskChannelFactory in a test fixture."
        )
    return _factory


def task_channel(task_id: int) -> TaskChannel:
    """Return a :class:`TaskChannel` scoped to *task_id* via the active
    factory. Connection lifecycle is owned by the factory, not the
    caller."""
    return get_task_channel_factory().for_task(task_id)


async def task_id_for_telegram_message(message_id: int) -> int | None:
    """Look up the task a previously-bound Telegram message belongs to.

    Module-level (not on :class:`TaskChannel`) because the read happens
    *before* a ``task_id`` is known — the ``message_id`` is the lookup
    key. Forcing the caller to instantiate a per-task handle just to
    read would be a worse abstraction.
    """
    return await get_task_channel_factory().task_id_for_telegram_message(message_id)
