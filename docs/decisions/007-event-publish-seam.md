# [ADR-007] Event publishing as a real seam

## Status

Accepted

## Context

Event publishing was a free-function pair (`get_redis` + `publish_event`)
plus a manual connection lifecycle. Every emitter wrote the same
three-line dance:

```python
r = await get_redis()
await publish_event(r, Event(type=..., task_id=...).to_redis())
await r.aclose()
```

We had ~80 of these triplets across `agent/`, `claude_runner/`,
`orchestrator/`, `integrations/`, and `web/`. The dance opened and closed
a TCP connection per publish (since `aioredis.from_url` returns a fresh
client each time), nested awkwardly inside `try/except` blocks, and forced
every caller to know that events go to Redis Streams.

The consumer side already had a real abstraction (`EventBus` in
`shared/events.py`) — `run.py` reads the stream once and dispatches into
the bus. There was no symmetric production publish seam.

Tests felt the pain first: every publisher test had to monkey-patch both
`get_redis` and `publish_event` (`tests/test_delete_repo.py`,
`tests/test_task_messages.py`), and assertions had to fish through mock
call args to recover the event payload. An in-memory adapter has been
implicitly wanted for a long time.

## Decision

Promote `shared/events.py` from a consumer-only module into the project's
publish seam:

- `class Publisher(Protocol)` — `async publish(event)` + `async aclose()`.
- `class RedisStreamPublisher` — production adapter. Lazy-instantiates a
  single long-lived `redis.asyncio.Redis` client (which already pools
  connections) and `xadd`s every event to one stream key. No per-call
  open/close.
- `class InMemoryPublisher` — test adapter. Captures published events into
  a list and exposes `wait_for(event_type)` for cross-task assertions.
- Module-level `set_publisher(p)` / `get_publisher()` / `await publish(event)`
  delegate to whichever publisher is registered. `run.py`'s `lifespan`
  registers the Redis adapter at startup; `tests/conftest.py` registers a
  fresh `InMemoryPublisher` per test via an autouse fixture.

`publish_event` is removed from `shared/redis_client.py` entirely —
publishers no longer touch Redis. The consumer-side helpers
(`get_redis`, `read_events`, `ack_event`, `ensure_stream_group`) stay;
they remain the consumer-side seam.

## Consequences

**Wins.**
- Every publisher collapses from three lines + nested `try/except` to one
  line: `await publish(Event(type=..., task_id=...))`.
- Tests no longer monkey-patch Redis; they assert against `publisher.events`.
  An autouse fixture guarantees no test ever opens a real Redis
  connection.
- One TCP connection per process for publishes, not one per event.

**Trade-offs.**
- One global publisher. `set_publisher` is a process-level switch — fine
  for a single-process app, but if we ever run multiple publishers in the
  same process they would step on each other. The deletion test holds
  even then: the alternative is restoring the connection lifecycle to 80
  call sites.
- Non-publish Redis uses (`r.publish(stream)`, `r.lpop(guidance)`,
  `r.set(heartbeat)`, `r.set(telegram_msg_map)`, the consumer-side stream
  reader) intentionally still go through `shared/redis_client.get_redis()`.
  A future "Redis state seam" could deepen those, but bundling them now
  would balloon the diff and weaken the two-adapter justification.

**Alternatives rejected.**
- *Add `publish` to `EventBus`.* `EventBus` is an in-process dispatcher;
  conflating in-process dispatch with cross-process Redis publish would
  muddy two distinct concerns. Keeping them separate keeps each module
  deep but single-purpose.
- *Inject the publisher as a parameter to every emitter.* Threading a
  publisher through the call graph (often four or five layers down into
  webhook handlers and pollers) would re-introduce shallow plumbing
  exactly where we wanted to remove it. A module-level seam matches the
  symmetry with `EventBus.dispatch` (also module-level on the consumer).
