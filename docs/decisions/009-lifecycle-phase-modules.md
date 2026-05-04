# [ADR-009] Split `agent/main.py` into per-phase task lifecycle modules

## Status

Accepted

## Context

`agent/main.py` had grown to 1,957 lines owning every phase of the task
lifecycle: branch slugify (LLM + fallback), PR-title generation, intent
extraction, repo-summary generation, planning (with grill phase, plan
revision, clarification extraction, GRILL_DONE handling, hard grill cap),
coding (single + multi-phase subtask paths), self-review with retry, gh-CLI
PR creation/find, independent review, plan-only review, plan conversation,
clarification response routing, blocked-task routing, deploy preview
(GitHub workflow + local script), query handler, harness onboarding shim,
PO worker, and the `event_loop` that dispatched to all of them. State of
in-flight conversations was held in three module-level sets
(`_active_planning`, `_active_clarification_tasks`,
`_active_plan_conversations`). The `event_loop` was a 100-line `if/elif`
chain over `event.type` — the kind of dispatcher that turns into a magnet
for "while you're in there" additions.

ADR-008 had just collapsed `claude_runner/` into `agent/`, deleting ~2,300
lines of duplicate code, leaving a single source of truth for the
lifecycle but no internal structure within it. With one canonical location
the file size was no longer hidden behind duplication, and the cost of
adding a new phase or modifying an existing one was concentrated in one
place.

`shared/events.py` already shipped an `EventBus` with glob pattern
matching — but it was consumer-only; nothing dispatched events through it.
The `Publisher` seam introduced in ADR-007 had production
(`RedisStreamPublisher`) and test (`InMemoryPublisher`) adapters; the
dispatch side had only ever been the in-line `elif` chain in
`event_loop`.

## Decision

Carve `agent/main.py` into per-phase modules under `agent/lifecycle/`,
each owning one phase's handler, helpers, and module-private state, with
dispatch wired through the existing `EventBus`.

**Layout**:

```
agent/lifecycle/
├─ planning.py           # handle_planning + grill state machine + _active_planning
├─ coding.py             # handle_coding + single/subtasks + _finish_coding
├─ review.py             # find_existing_pr_url, create_pr, handle_independent_review,
│                        # handle_plan_independent_review, handle_pr_review_comments
├─ deploy.py             # handle_deploy_preview + workflow + local script
├─ query.py              # handle_query (SIMPLE_NO_CODE single-LLM-call answer)
├─ cleanup.py            # handle_task_cleanup
├─ conversation.py       # plan-conversation + clarification + blocked + human.message
│                        # route_human_message (status-based dispatcher)
├─ po_worker.py          # _po_queue + _po_worker + start() + handle(event)
├─ harness_onboard.py    # thin handle(event) → agent.harness shim
├─ intent.py             # extract_intent + INTENT_EXTRACTION_PROMPT
├─ _orchestrator_api.py  # get_task, get_repo, get_freeform_config, transition_task
├─ _agent.py             # create_agent factory + UI streaming hooks
├─ _naming.py            # slugify, branch name, PR title, _session_id, _fresh_session_id
└─ _clarification.py     # _extract_clarification (CLARIFICATION_MARKER)
```

Each lifecycle module exposes `async def handle(event: Event) -> None`
matching the existing `EventHandler` protocol from `shared/events.py`.
`agent/main.py` (now 95 lines) wires them up:

```python
def register_handlers(bus: EventBus) -> None:
    bus.on("task.start_planning",          planning.handle)
    bus.on("task.plan_ready",              review.handle_plan_ready)
    bus.on("task.start_coding",            coding.handle)
    bus.on("task.deploy_preview",          deploy.handle)
    bus.on("task.query",                   query.handle)
    bus.on("task.cleanup",                 cleanup.handle)
    bus.on("task.clarification_response",  conversation.handle_clarification_event)
    bus.on("po.analyze",                   po_worker.handle)
    bus.on("repo.onboard",                 harness_onboard.handle)
    bus.on("human.message",                conversation.route_human_message)
```

`event_loop` reads from Redis Streams, decodes each message, and calls
`bus.dispatch(event)`. The `if/elif` chain is gone.

**No `Result` return type.** The original task brief floated `handle(task)
→ terminal-state Result`. The deletion test killed it: every existing
handler drives state via `await transition_task(...)` + `await publish
(...)`. A `Result` type would have no consumer — the orchestrator state
machine and the bus already do the routing. The handler signature is
`async def handle(event: Event) -> None`, exactly matching `EventHandler`.

**Centralised registration over decentralised import-time side effects.**
`register_handlers(bus)` calls each module's registration explicitly;
modules don't self-register at import. This keeps import order debuggable
and lets tests build a bus without spinning up production wiring (see
`tests/test_lifecycle_dispatch.py`).

**Cross-phase dependencies as direct imports.** `coding._finish_coding`
calls `review.handle_independent_review`; `conversation.
handle_blocked_response` dispatches into planning/coding/review;
`conversation.handle_clarification_response` re-enters planning for grill
resume. Direct calls (not bus round-trips) — these are intra-handler
control flow; the bus is for entry from Redis Streams, not for re-routing.

**Module-level state stays module-level.** `_active_planning` lives in
`planning.py`; `_active_plan_conversations` and `_active_clarification_tasks`
live in `conversation.py`. They are guards against re-entrant in-process
invocations, which is a per-module concern — lifting them into a shared
registry would create coupling without payoff.

**The Redis stream consumer name `consumer="claude-runner"` is preserved**
as a stable wire-protocol identifier (per ADR-008), even though the
`claude_runner` package is gone. Renaming would orphan in-flight stream
entries.

## Consequences

**Wins.**

- `agent/main.py` shrinks from 1,957 to 95 lines. Every lifecycle module is
  under the 500-line guideline.
- New event types must be registered in `register_handlers(bus)` — there's
  one place to look. `tests/test_lifecycle_dispatch.py` parametrizes over
  every registered event type and asserts the right handler is wired,
  catching future regressions where a new lifecycle module is added but
  never registered.
- The EventBus's glob pattern matching is finally exercised in production —
  ADR-007 introduced the `Publisher` seam; this ADR finishes the dispatch
  seam.
- Module-private state stays close to the handler that mutates it; no
  shared globals across modules.
- Each phase reads as a focused module: planning grills, coding implements,
  review reviews. No more "find the relevant section in the 1,957-line
  file."

**Trade-offs.**

- Cross-phase direct imports (`coding._finish_coding` → `review.handle_independent_review`)
  are still tightly coupled — the bus does NOT decouple them. We
  considered routing intra-handler control flow through the bus, but that
  would have introduced extra Redis round-trips and made traces harder to
  follow. The bus is for entry from Redis; direct calls are for in-process
  fan-out. This is the same pattern `shared/events.py::publish()`
  established (cross-process) vs in-process function calls.

- Some `agent.main` test patch paths needed updating (covered in the test
  diff). Per ADR-008's no-shallow-facade stance, we did NOT add re-export
  shims in `agent/main.py` — tests import from the canonical location.

**Alternatives rejected.**

- *Keep `agent/main.py` and just split into private helper modules under
  `agent/_main_helpers/` while leaving `agent/main.py` as the dispatcher.*
  Rejected: this would have produced a thin file that calls into a deeper
  one — the shallow-wrapper anti-pattern. The deletion test on a wrapper
  layer says "delete it, what breaks?" Nothing — and that's the point.

- *Introduce a `Result` return type for handlers.* Rejected (deletion
  test). State is driven by side effects (`transition_task`, `publish`);
  a `Result` would have no consumer.

- *Have each module self-register on import via a decorator
  (`@bus.on("task.start_planning")`).* Rejected: import-order side effects
  make tests harder (you can't construct a clean bus) and obscure the
  list of wired handlers (there's no single place to look). Explicit
  `register_handlers` is shorter, debuggable, and unit-testable.

- *Split `_finish_coding` between coding and review.* Rejected: the
  self-review loop, auto-commit safety net, push, and PR creation are
  one cohesive flow. Splitting would create artificial seams where the
  domain has none.
