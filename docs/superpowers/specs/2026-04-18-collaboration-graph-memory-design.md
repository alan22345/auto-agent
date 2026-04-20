# Collaboration & Graph Memory Design

## Summary

Expand auto-agent from a single-user tool to a small-team collaboration platform (2-5 people) with shared graph memory that persists project knowledge, decisions, and team preferences across tasks and projects.

## Requirements

1. **Per-user authentication** — username/password login, JWT sessions
2. **Shared visibility** — all users see all tasks, messages, and can interact with any task
3. **User attribution** — messages and tasks show who created/sent them
4. **Graph memory** — Postgres adjacency list storing projects, capabilities, decisions, and preferences as LLM-named nodes with typed edges
5. **Decision chains** — append-only history of decisions linked via `evolved-from` edges; never overwrite, always append
6. **Cross-project intelligence** — agent discovers connections between projects via shared capabilities (e.g., data-generator produces financial-reports, new UI project consumes them)
7. **Memory supplements CLAUDE.md** — graph memory is injected alongside CLAUDE.md, not replacing it

## Architecture

### Auth & User Model

New `User` ORM model: `id`, `username` (unique), `password_hash`, `display_name`, `created_at`, `last_login`.

- Passwords hashed with bcrypt via passlib
- Login via `/api/auth/login` returns JWT in httpOnly cookie
- WebSocket connections authenticated via same cookie
- First user seeded via env vars (`AUTO_AGENT_ADMIN_USER` / `AUTO_AGENT_ADMIN_PASS`)
- Any user can create new users (simple form, no email verification)
- `Task` gains `created_by_user_id` FK
- Replace existing HTTP Basic auth entirely

No roles or permissions — all users have equal access.

### Graph Memory Schema

Two tables: `MemoryNode` and `MemoryEdge`.

```
MemoryNode:
  id: UUID
  name: str              # LLM-generated
  node_type: str          # LLM-generated: "project", "capability", "decision", "preference", etc.
  content: text           # Free-form text
  created_at: datetime
  updated_at: datetime
  created_by_task_id: FK  # Which task created this node

MemoryEdge:
  id: UUID
  source_id: FK -> MemoryNode
  target_id: FK -> MemoryNode
  relation: str           # LLM-generated: "decided", "produces", "consumes", "evolved-from", etc.
  created_at: datetime
```

Design principles:
- Node names, types, and edge relations are all LLM-generated (no enums) for better semantic understanding
- Root nodes have no incoming edges
- Decisions are append-only chains linked by `evolved-from` edges (leaf = oldest, head = newest)
- Projects advertise capabilities they produce/consume, enabling cross-project discovery
- Amendments append new decision nodes rather than editing existing ones
- Corrections to non-decision content use `update_node`

Example graph:
```
(data-generator) --[produces]--> (capability:financial-reports)
(new-ui-project) --[consumes]--> (capability:financial-reports)

(data-generator) --[decided]--> (decision: "use pandas for transforms, 2024-03")
                                   --[evolved-from]--> (decision: "switched to polars, 2024-09")
                                                          --[evolved-from]--> (decision: "added duckdb, 2025-01")
```

### Agent Memory Tools

**`memory_read.py`:**
- `search(query)` — keyword search across node names and content
- `traverse(node_id, depth)` — walk edges N levels deep
- `get_decision_chain(node_id)` — follow evolved-from edges, newest to oldest
- `list_roots()` — return all root nodes (no incoming edges)

**`memory_write.py`:**
- `create_node(name, node_type, content)` — create a node
- `create_edge(source_id, target_id, relation)` — link two nodes
- `append_decision(parent_node_id, content, relation="evolved-from")` — add to a decision chain
- `update_node(node_id, content)` — amend content (corrections only, not decisions)
- `delete_node(node_id)` — remove node and its edges

### Memory Integration Points

- **Task start:** Context manager queries graph with task keywords, injects relevant nodes into system prompt
- **During execution:** Memory tools available like any other tool for mid-task queries
- **Task completion:** Agent prompted to reflect and record new decisions/capabilities
- **On user request:** Explicit "remember X" triggers immediate write

No automatic writes during task execution — only at completion or on request.

### Collaboration UX

- WebSocket connections authenticated, messages carry user identity
- Chat shows display names on all messages
- Tasks show creator in sidebar
- Any user can approve/reject/send guidance to any task
- Simple user management page (create users, change own password)
- No dedicated memory UI in v1 — interact with memory through the agent

## New Dependencies

- `passlib[bcrypt]` — password hashing
- `PyJWT` — session tokens

## Files Changed

| Area | File | Change |
|------|------|--------|
| Models | `shared/models.py` | Add User, MemoryNode, MemoryEdge |
| Types | `shared/types.py` | Add user/memory Pydantic models |
| Auth | `orchestrator/auth.py` | New — login, token validation, middleware |
| Web | `web/main.py` | Auth middleware, user context in messages |
| Web | `web/static/index.html` | Login screen, user attribution, user management |
| Tools | `agent/tools/memory_read.py` | New — graph query tools |
| Tools | `agent/tools/memory_write.py` | New — graph mutation tools |
| Tools | `agent/tools/__init__.py` | Register memory tools |
| Context | `agent/context/system.py` | Inject memory into system prompt |
| Context | `agent/context/memory.py` | New — graph query for task context |
| Agent | `agent/main.py` | Post-task memory reflection |
| Migration | `migrations/versions/` | New migration for all new tables |
| Task model | `shared/models.py` | Add created_by_user_id FK to Task |
