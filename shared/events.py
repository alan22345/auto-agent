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
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


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
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

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

from collections.abc import Callable, Awaitable  # noqa: E402
import fnmatch  # noqa: E402

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
