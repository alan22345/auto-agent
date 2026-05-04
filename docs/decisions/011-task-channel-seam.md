## [ADR-011] Per-task Redis state as a real seam (TaskChannel)

## Status

Accepted

## Context

ADR-007 deepened the broadcast-event publisher into a real
``Publisher`` seam. It explicitly deferred the per-task Redis surface:

> Non-publish Redis uses (`r.publish(stream)`, `r.lpop(guidance)`,
> `r.set(heartbeat)`, `r.set(telegram_msg_map)`, the consumer-side
> stream reader) intentionally still go through
> `shared/redis_client.get_redis()`. A future "Redis state seam"
> could deepen those, but bundling them now would balloon the diff
> and weaken the two-adapter justification.

Since then those four key strings — ``task:{id}:guidance``,
``task:{id}:heartbeat``, ``task:{id}:stream``, and
``telegram:msg:{id}`` — were referenced from nine call sites across
five files (``agent/lifecycle/factory.py``,
``agent/lifecycle/conversation.py``, ``orchestrator/router.py``,
``run.py``, ``shared/notifier.py``, ``integrations/telegram/main.py``,
``web/main.py``). Every site re-ran the same lifecycle dance:

```python
r = await get_redis()
await r.<op>(f"task:{task_id}:<key>", ...)
await r.aclose()
```

each wrapped in its own ``try/except``. The watchdog, the live-stream
UI, and the guidance queue shared only a stringly-typed key
convention, and ``tests/test_task_messages.py`` was already paying
the cost — it monkey-patched ``orchestrator.router.get_redis`` with
an ``AsyncMock`` to assert against ``rpush`` call args. The second
adapter ADR-007 cited as future work was already real.

## Decision

Promote the per-task Redis surface to a seam in ``shared/task_channel.py``,
mirroring the ADR-007 ``Publisher`` shape:

- ``class TaskChannel(Protocol)`` — seven verbs scoped to one task:
  ``push_guidance``, ``pop_guidance``, ``heartbeat``, ``is_alive``,
  ``stream_tool_call``, ``stream_thinking``, ``bind_telegram_message``.
- ``class TaskChannelFactory(Protocol)`` — owns the shared resource
  (Redis client / in-memory dicts) and returns per-task handles via
  ``for_task(task_id)``. Also exposes ``task_id_for_telegram_message``
  (see asymmetry note below) and ``aclose``.
- ``RedisTaskChannelFactory`` — production adapter. Lazy-instantiates
  one long-lived ``redis.asyncio.Redis`` client (which already pools
  connections), and per-task ``RedisTaskChannel`` instances issue
  commands against that shared client.
- ``InMemoryTaskChannelFactory`` — test adapter. Captures pushes,
  heartbeats, streams, and telegram bindings in inspectable dicts and
  lists; ``InMemoryTaskChannel`` instances delegate to them.
- Module-level ``set_task_channel_factory`` / ``get_task_channel_factory`` /
  ``task_channel(task_id)`` / ``task_id_for_telegram_message(message_id)``
  delegate to whichever factory is registered. ``run.py``'s lifespan
  registers ``RedisTaskChannelFactory`` next to ``RedisStreamPublisher``
  and ``aclose``s both on shutdown; ``tests/conftest.py`` installs a
  fresh ``InMemoryTaskChannelFactory`` per test via an autouse fixture.

The four key strings, the 15-minute heartbeat TTL, the 7-day
telegram-binding TTL, and the ``{"type": ..., **payload}`` JSON wire
format for streamed events all live in this one module.

**Deliberate asymmetry: ``bind_telegram_message`` vs.
``task_id_for_telegram_message``.** The bind side is an instance
method on ``TaskChannel`` because the writer has a ``task_id``. The
lookup side is module-level — at lookup time the caller has no
``task_id``; the ``message_id`` *is* the lookup key. Forcing the
caller to instantiate a per-task handle just to read would be a
worse abstraction. This matches the consumer-side asymmetry of
``EventBus.dispatch`` / ``publish`` from ADR-007: the right shape
follows the call graph, not symmetry for its own sake.

The wildcard subscriber in ``web/main.py::agent_stream_listener``
stays a stream reader (it has no ``task_id`` at subscribe time) but
imports ``TASK_STREAM_PATTERN`` from the seam so even the wildcard
doesn't hard-code the prefix.

## Consequences

**Wins.**

- Every per-task Redis call site collapses to a single line. Nine
  ``get_redis()/op/aclose()`` blocks (with their try/except scaffolding)
  are gone; three shallow wrappers in ``agent/lifecycle/factory.py``
  and one in ``run.py`` deleted by the deletion test.
- Tests no longer monkey-patch ``get_redis``. The autouse
  ``task_channel`` fixture (mirroring ``publisher``) gives every test
  a fresh ``InMemoryTaskChannelFactory``, and assertions become
  direct: ``assert task_channel.guidance[1] == [...]``. ``tests/test_task_messages.py``
  picked up exactly the simplification ADR-007 promised.
- One TCP connection per process for per-task ops, not one per call —
  same win the publisher seam delivered.
- The four key strings, the heartbeat TTL, and the streamed-event JSON
  shape have exactly one home. Future renames are ``rg``-confirmed
  one-file changes.

**Trade-offs.**

- One global factory. ``set_task_channel_factory`` is a process-level
  switch — fine for a single-process app, but if we ever run multiple
  factories in the same process they would step on each other. Same
  trade-off ADR-007 accepted; the deletion test holds either way.
- The bind/lookup asymmetry could surprise a reader who expects every
  per-task verb on ``TaskChannel``. Documented in this ADR and in the
  module docstring.

**Alternatives rejected.**

- *Extend ``Publisher`` to carry per-task state.* ``Publisher`` is the
  cross-process broadcast-event seam (one stream, every consumer
  group reads it). Per-task state has a different lifecycle (KV with
  TTLs, FIFO list, point-to-point pubsub). Conflating them would
  muddy two distinct concerns. Same calculus that ADR-007 used to
  keep ``Publisher`` and ``EventBus`` separate.
- *Inject a ``TaskChannel`` parameter into every emitter.* Threading
  a per-task handle through the call graph (often four or five
  layers down into webhook handlers and pollers) would re-introduce
  the shallow plumbing this seam removes. A module-level facade
  matches the symmetry with ``publish()``.
- *Keep the helpers (``_stream_to_task``, ``_check_guidance``,
  ``_heartbeat_for_task``, ``_task_has_heartbeat``,
  ``_task_id_for_message``) as a per-file API.* Failed the deletion
  test — each was a one-caller (or two-caller) wrapper around
  ``get_redis()/op/aclose()`` with no logic of its own. The verb on
  ``TaskChannel`` is the deeper module they were forwarding to;
  removing the wrapper concentrates no complexity at the call site.

This ADR supersedes ADR-007's "future state seam" footnote.
