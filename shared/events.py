"""Flexible event system for inter-service communication via Redis Streams.

Events use string-based types with a `domain.action` convention instead of a
closed enum.  Any service can emit or subscribe to any event pattern without
modifying a central definition.

Convention:
    task.created        — a new task was ingested
    task.status_changed — a task transitioned to a new status
    task.classified     — complexity classification finished
    task.code_complete  — Claude Code finished writing + pushed a branch
    human.message       — user sent a message (WhatsApp / Slack / GitHub review)
    notify.send         — request to send a notification to the user

These are conventions, not constraints — any string works.

Two seams live in this module:
  - ``Event`` + ``EventBus`` — in-process dispatcher used by consumers.
  - ``Publisher`` + ``publish()`` — the cross-process publish seam used by
    every emitter. ``RedisStreamPublisher`` is the production adapter;
    ``InMemoryPublisher`` is the test adapter. Callers never see Redis.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Stream key — the single Redis Streams key all events are written to and
# read from. Lives here (not in shared/redis_client) so the consumer-side
# helpers and the publisher both import from one source of truth.
# ---------------------------------------------------------------------------

STREAM_KEY = "autoagent:events"


class Event(BaseModel):
    """A single event on the bus.

    Attributes:
        type:     Free-form string following ``domain.action`` convention.
        task_id:  Optional task this event relates to.
        payload:  Arbitrary data — consumers should validate what they need.
        timestamp: When the event was created (UTC).
    """

    type: str
    task_id: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # --- Redis serialisation helpers ---

    def to_redis(self) -> dict[str, str]:
        return {
            "type": self.type,
            "data": json.dumps(self.model_dump(mode="json")),
        }

    @classmethod
    def from_redis(cls, data: dict[bytes | str, bytes | str]) -> Event:
        raw = data.get(b"data") or data.get("data")
        if isinstance(raw, bytes):
            raw = raw.decode()
        return cls.model_validate_json(raw)


# ---------------------------------------------------------------------------
# Handler registry
#
# Services register async handler functions against *glob-style* event
# patterns.  The EventBus dispatches incoming events to every matching
# handler.
#
# Patterns:
#   "task.created"      — exact match
#   "task.*"            — matches task.created, task.classified, …
#   "*"                 — matches everything
# ---------------------------------------------------------------------------

EventHandler = Callable[[Event], Awaitable[None]]


class EventBus:
    """In-process event dispatcher with glob pattern matching."""

    def __init__(self) -> None:
        self._handlers: list[tuple[str, EventHandler]] = []

    def on(self, pattern: str, handler: EventHandler) -> None:
        """Register *handler* for events whose type matches *pattern*."""
        self._handlers.append((pattern, handler))

    async def dispatch(self, event: Event) -> None:
        """Dispatch *event* to all handlers whose pattern matches."""
        for pattern, handler in self._handlers:
            if fnmatch.fnmatch(event.type, pattern):
                await handler(event)


# ---------------------------------------------------------------------------
# Publisher seam
#
# Production code calls ``await publish(event)`` — a single line. The
# module-level helper delegates to whichever Publisher was registered at
# startup. Tests swap in ``InMemoryPublisher`` via ``set_publisher`` so they
# can assert against captured events without touching Redis.
# ---------------------------------------------------------------------------


class Publisher(Protocol):
    """Anything that can accept an Event for downstream consumers."""

    async def publish(self, event: Event) -> None: ...

    async def aclose(self) -> None: ...


class RedisStreamPublisher:
    """Production adapter — owns one long-lived ``redis.asyncio.Redis`` client.

    The previous pattern opened and closed a TCP connection per publish, which
    is what every caller's ``r = await get_redis() / await r.aclose()`` dance
    was working around. ``redis.asyncio.Redis`` already pools connections
    internally, so a single lazy-instantiated client suffices for the lifetime
    of the process.
    """

    def __init__(self, url: str, stream_key: str = STREAM_KEY) -> None:
        self._url = url
        self._stream_key = stream_key
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

    async def publish(self, event: Event) -> None:
        client = await self._get_client()
        await client.xadd(self._stream_key, event.to_redis())

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class InMemoryPublisher:
    """Test adapter — captures published events for assertion.

    Use ``events`` to inspect every event published since construction. Use
    ``wait_for(type)`` to await an event of a given type when the publish
    happens in another task.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._waiters: list[tuple[str, asyncio.Future[Event]]] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)
        # Resolve any matching waiters
        remaining: list[tuple[str, asyncio.Future[Event]]] = []
        for pattern, fut in self._waiters:
            if not fut.done() and fnmatch.fnmatch(event.type, pattern):
                fut.set_result(event)
            else:
                remaining.append((pattern, fut))
        self._waiters = remaining

    async def wait_for(self, event_type: str, timeout: float = 1.0) -> Event:
        """Return the next event matching *event_type* (glob pattern allowed).

        Returns immediately if a matching event was already published.
        """
        for ev in self.events:
            if fnmatch.fnmatch(ev.type, event_type):
                return ev
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Event] = loop.create_future()
        self._waiters.append((event_type, fut))
        return await asyncio.wait_for(fut, timeout=timeout)

    def clear(self) -> None:
        self.events.clear()
        for _, fut in self._waiters:
            if not fut.done():
                fut.cancel()
        self._waiters.clear()

    async def aclose(self) -> None:
        self.clear()


_publisher: Publisher | None = None


def set_publisher(publisher: Publisher) -> None:
    """Register the active publisher. Called once at process start (production)
    and per-test (in fixtures).

    Does NOT aclose the previous publisher — if the caller is replacing a live
    one, it owns the cleanup. Production wires a single RedisStreamPublisher in
    ``run.py``'s lifespan and acloses it on shutdown; tests use a per-test
    fixture that restores the previous reference without calling this again.
    """
    global _publisher
    _publisher = publisher


def get_publisher() -> Publisher:
    """Return the active publisher. Raises if none is registered — every
    process must wire one in before publishing."""
    if _publisher is None:
        raise RuntimeError(
            "No Publisher registered. Call set_publisher(...) at startup, "
            "or install an InMemoryPublisher in a test fixture."
        )
    return _publisher


async def publish(event: Event) -> None:
    """Publish *event* through the active publisher.

    This is the public publish seam — every emitter calls this single
    function. Connection lifecycle is owned by the publisher, not the caller.
    """
    await get_publisher().publish(event)
