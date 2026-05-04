"""Agent event loop — Redis-Streams consumer that dispatches via EventBus.

Each per-phase module under ``agent/lifecycle/`` registers an
``async def handle(event)`` against an event-type pattern on the
``EventBus``. The loop reads events off the Redis stream, decodes them, and
hands each one to ``bus.dispatch(event)``.

The ``consumer="claude-runner"`` id is preserved as a stable wire-protocol
identifier (per ADR-008), even though the ``claude_runner`` package is gone.
"""

from __future__ import annotations

import asyncio

from agent.lifecycle import (
    cleanup,
    coding,
    conversation,
    deploy,
    harness_onboard,
    planning,
    po_worker,
    query,
    review,
)
from shared.events import (
    Event,
    EventBus,
    HumanEventType,
    POEventType,
    RepoEventType,
    TaskEventType,
)
from shared.logging import setup_logging
from shared.redis_client import (
    ack_event,
    ensure_stream_group,
    get_redis,
    read_events,
)

log = setup_logging("agent")


def register_handlers(bus: EventBus) -> None:
    """Register every lifecycle module's handler against its event pattern.

    Centralised registration is intentional: tests can build an empty bus,
    register a subset, and assert dispatch behaviour without spinning up the
    production wiring. Per-module self-registration via import-time side
    effects would make import order load-bearing.
    """
    bus.on(TaskEventType.START_PLANNING, planning.handle)
    bus.on(TaskEventType.PLAN_READY, review.handle_plan_ready)
    bus.on(TaskEventType.START_CODING, coding.handle)
    bus.on(TaskEventType.DEPLOY_PREVIEW, deploy.handle)
    bus.on(TaskEventType.QUERY, query.handle)
    bus.on(TaskEventType.CLEANUP, cleanup.handle)
    bus.on(TaskEventType.CLARIFICATION_RESPONSE, conversation.handle_clarification_event)
    bus.on(POEventType.ANALYZE, po_worker.handle)
    bus.on(RepoEventType.ONBOARD, harness_onboard.handle)
    bus.on(HumanEventType.MESSAGE, conversation.route_human_message)


async def event_loop() -> None:
    """Main loop — read events from Redis Streams and dispatch through the bus."""
    bus = EventBus()
    register_handlers(bus)
    po_worker.start()

    r = await get_redis()
    await ensure_stream_group(r)
    log.info("Agent event loop started")

    backoff = 1
    max_backoff = 60

    while True:
        try:
            messages = await read_events(r, consumer="claude-runner", count=1, block=5000)
            backoff = 1
            for msg_id, data in messages:
                try:
                    event = Event.from_redis(data)
                    await bus.dispatch(event)
                except Exception:
                    log.exception("Error handling event")
                finally:
                    await ack_event(r, msg_id, consumer="claude-runner")
        except Exception:
            log.exception("Event loop error", retry_in=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
            try:
                r = await get_redis()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(event_loop())
