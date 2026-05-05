# [ADR-011] Typed event taxonomy in shared/events.py

## Status

Accepted

## Context

ADR-007 deepened event publishing into a `Publisher` Protocol with a
`RedisStreamPublisher` production adapter and an `InMemoryPublisher`
test adapter. Every emitter collapsed to one line:

```python
await publish(Event(type="task.created", task_id=task.id))
```

But the *taxonomy* — what `type` values mean — stayed shallow. The
field was a free-form string with a documented `domain.action`
convention; producers wrote string literals and consumers compared
against string literals. Discovering the set of valid event types
required grepping the repo. A typo on either end (e.g.
`task.cancelled` vs `task.canceled`, or `payload={"resaon": ...}`)
silently misrouted; the failure mode was an event the consumer never
matched.

Concrete bug this surfaced: `orchestrator/webhooks/github.py` was
emitting `task.failed` with `payload={"reason": "PR closed without
merging"}`, but the Telegram dispatcher reads `payload.get("error",
"unknown")` for that event — so PR-close-failures showed up in
Telegram as "Error: unknown". The two sites had drifted apart with no
type system to catch it.

The shape of the consumer side amplified the problem:
`integrations/telegram/main.py` had a 23-branch if/elif chain over
`event.type`, plus a parallel `NOTIFY_EVENTS` frozenset that had to be
kept in sync with the chain by hand.

## Decision

Add the taxonomy to the same module that owns the transport
(`shared/events.py`), along an axis orthogonal to ADR-007:

- **Enums.** `TaskEventType`, `POEventType`, `ArchitectureEventType`,
  `RepoEventType`, `HumanEventType` as `StrEnum` subclasses. Members
  exhaust the events the system emits today; the wire string lives
  exactly once, on the enum member's value.

- **Factories.** One factory function per event:
  `task_created(task_id)`, `task_ci_failed(task_id, reason=…)`,
  `human_message(task_id, message=…, source=…)`, etc. The factory
  signature encodes the payload schema, so a typo in a payload key is
  a `TypeError` at the producer call site instead of a silent miss
  downstream.

- **Wire format unchanged.** `StrEnum` is a `str` subclass; the JSON
  serialisation path (`model_dump(mode="json")`) emits the bare wire
  string. A consumer comparing `event.type == "task.created"` keeps
  matching, so producers and consumers can be migrated independently.

- **`Event.type` stays `str`.** Three reasons: (1) `EventBus` matches
  glob patterns like `"task.*"` — patterns aren't enum members, so the
  field must accept strings; (2) consumers reading off the Redis
  stream may legitimately receive an event type added after they were
  last deployed and `from_redis` must not crash on it; (3) keeping
  `type: str` preserves `StrEnum`'s subclass-of-`str` substitutability
  — assigning the enum member works without `.value`.

- **Telegram dispatcher.** The 23-branch if/elif chain becomes
  `_NOTIFICATION_FORMATTERS: dict[StrEnum, Callable]`. Each formatter
  is a small pure function `(payload, task_info, is_freeform,
  task_id) → str`. The parallel `NOTIFY_EVENTS` frozenset is deleted;
  the dict keys *are* the set.

## Consequences

**Wins.**

- One module owns every event the system can emit.
- Adding a new event is one enum member + one factory + (if the
  Telegram dispatcher should render it) one dict entry.
- Tests assert against constructors, not magic strings.
- A typo on either end becomes a `NameError` (`TaskEventType.CRAETED`)
  or a `TypeError` (`task_ci_failed(task_id, resaon=…)`) at producer
  time.
- Root-cause bug fix dropped out of the migration: the
  `task.failed`-payload divergence between `orchestrator/webhooks/github.py`
  and `run.py` collapsed onto a single `task_failed(task_id, error=…)`
  factory.

**Trade-offs.**

- A list of 27 enum members + 27 factories is more code than 27 string
  literals. The deletion test holds: removing the enums would
  re-scatter the strings across 35+ producer sites and 30+ consumer
  branches. Adding factories for trivial `(task_id)`-only events is
  mostly a stylistic uniformity argument, but it pays off when a
  payload field gets added later — the change ripples to one
  factory-signature, not 5 producer call sites.
- `Event.type` can still be assigned a free-form string (anything you
  pass to `Event(type=...)` works). The enum guides emitters; it
  doesn't constrain `Event` itself. That's intentional (see "stays
  `str`" above) but means the typo guarantee is one-sided: producers
  using factories can't typo, but a producer that bypasses factories
  and writes `Event(type="task.cretaed")` will still slip through.

**Alternatives rejected.**

- *Split into `shared/event_types.py`.* Two shallow neighbour modules
  (`shared/events` for transport + dispatch, `shared/event_types` for
  enums and factories) versus one deep module that owns both.
  Producers would have to import from both, every time. The taxonomy
  *is* what the transport carries; co-locating them is the deeper
  module.
- *Constrain `Event.type` to `TaskEventType | POEventType | …`.* The
  glob-pattern matcher (`"task.*"`) and the from-wire deserialiser
  (which must accept future event types) both require `str`. A union
  type would force every glob pattern site to convert.
- *Drop the trivial `(task_id)`-only factories and just expose the
  enum.* Mixed call sites — half `Event(type=TaskEventType.X,
  task_id=...)`, half `task_y(task_id, foo=...)` — make the seam
  inconsistent. Uniform factories pay the small typing tax for a
  single visual shape across the whole codebase.
