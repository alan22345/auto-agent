# [ADR-002] Memory browser uses the existing WebSocket contract for read + write

## Status

Accepted

## Context

The Memory tab was write-only (extract → review → save). Users had no way to
see what was already stored in team-memory before adding a new fact, so
duplicate/conflicting facts were easy to introduce and the only way to fix a
stale fact was to know its UUID and use the CLI.

The task description suggested two transport options for the new browse/search
panel:

- A REST `GET` endpoint backed by `recall_entity`.
- New WebSocket message types: `memory_search`, `memory_get_entity`,
  `memory_correct_fact`, `memory_delete_fact`.

For per-fact mutations (Edit / Correct / Delete) there is no benefit to REST —
the panel already has a live WS connection and the rest of the Memory tab
(extract / save) goes through it. For search/recent there is a mild trade-off:
GET would compose with TanStack Query (caching, retry); WS keeps the panel on
one channel and matches the existing pattern.

## Decision

Add four new WebSocket message types — `memory_search`, `memory_get_entity`,
`memory_correct_fact`, `memory_delete_fact` — and keep the entire Memory tab
on a single transport. The handlers live next to the existing
`_handle_memory_*` handlers in `web/main.py`. Read-side helpers
(`search_entities`, `list_recent_entities`, `get_entity_with_facts`,
`delete_fact`) live in `shared/memory_io.py` next to the existing
`recall_entity`/`remember_row`/`correct_fact` so all team-memory access stays
behind one seam that tests can mock.

`delete_fact` is implemented as a soft delete: it sets `valid_until = now()`
on the existing `Fact` row without creating a successor. `GraphEngine` only
exposes `correct` (which always creates a replacement) and we want a true
"end this fact" primitive that keeps the audit trail without leaving a
"(deleted)" placeholder current fact.

## Consequences

- The Memory tab stays on a single transport; one less concept to learn when
  reading the code.
- We give up TanStack Query's caching/retry on the search results. In
  practice the search is fast (single `recall` call, capped at 20 results) and
  the WS reconnect logic in `web-next/lib/ws.ts` already handles connection
  blips, so the gap is small.
- Tests that exercise the read-side handlers follow the same `FakeWS` pattern
  used for the existing memory handlers (`tests/test_memory_ws_handlers.py`).
- `delete_fact` bypasses `GraphEngine.correct`, so future GraphEngine changes
  to correction semantics won't automatically propagate to deletion. If a
  native delete primitive is added upstream we should switch to it and remove
  this wrapper.
- The recent-entities query talks to the team-memory tables directly through
  the same async session GraphEngine uses. This is the only place outside
  `team_memory.graph` that touches those tables; if the schema changes we
  need to update both sides.
