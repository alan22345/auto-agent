# Messenger conversation continuity (Slack + future channels)

**Date:** 2026-05-12
**Status:** Draft — awaiting user review
**Scope (v1):** Slack DM. Designed to extend to Telegram and other messengers without schema change.

## Problem

The Slack DM assistant (`agent/slack_assistant.py`) keeps per-user conversation state in a Python `dict` in process memory, with a 30-minute idle TTL and a 30-message cap. As a result:

- Any process restart (deploy, crash, OOM) wipes every in-flight conversation. Users mid-drafting a task see the assistant act like it's never met them. This was reproduced on 2026-05-12 — five deploys during the day left an active drafting session unrecoverable after four turns.
- The assistant has no concept of "we are talking about task X". Once a task is created, the DM stream forgets the linkage; the only task-scoped affordance is replying inside Slack threads on task notifications, which the user rarely uses for back-and-forth.
- There is no parity with the web UI behaviour: clicking a task in the UI resumes that task's conversation. Slack offers no equivalent.

## Goals

1. Slack DM conversations survive process restarts.
2. The assistant maintains a "current focus" per user: a pointer to the task the user is talking about right now. Sticky by default, with explicit switching.
3. Switching focus is handled by a deterministic, LLM-free orchestration step so freeform Slack chatter does not pollute the agent's task pipeline.
4. Source-agnostic from day one — same plumbing serves Slack today and Telegram (and others) later without schema change.

## Non-goals (v1)

- Freeform-mode and PO-analysis focus kinds. The data model accepts them via a `focus_kind` column but no inbox is wired. Deferred to v2.
- Migrating in-flight in-memory sessions. They are lost on deploy; users see the picker on their next message.
- Cross-source conversation merging. Each messenger keeps its own message history under the same focus. Focus itself is shared across sources.

## Design

### Architecture

```
Slack event                Telegram event (future)
       \                  /
        \                /
         v              v
   ┌────────────────────────┐
   │ messenger_router       │  ← new (orchestrator/messenger_router.py)
   │  - load user_focus     │
   │  - TTL expiry check    │
   │  - regex switch detect │
   │  - picker (no LLM)     │
   │  - persistence         │
   │  - draft→task rebind   │
   └────────────┬───────────┘
                │ when LLM needed
                v
   ┌────────────────────────┐
   │ slack_assistant.converse│ ← existing, signature updated
   │  - LLM tool loop only  │
   │  - returns appended    │
   │    messages + reply    │
   └────────────────────────┘
```

Slack's `integrations/slack/main.py` calls `messenger_router.handle(source='slack', user_id, text, thread_ts)`. Telegram, when added, calls the same entry point with `source='telegram'`.

### Data model

Single Alembic revision adds two tables.

```sql
-- One row per (user, source, focus). Holds the chat history.
CREATE TABLE messenger_conversations (
  id              BIGSERIAL PRIMARY KEY,
  user_id         BIGINT NOT NULL REFERENCES users(id),
  source          TEXT   NOT NULL,                -- 'slack', 'telegram', ...
  focus_kind      TEXT   NOT NULL,                -- 'draft' | 'task' (v1)
  focus_id        BIGINT,                         -- NULL for draft; task.id for task
  messages_json   JSONB  NOT NULL DEFAULT '[]',   -- list of Message dicts, capped at 200
  last_active_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, source, focus_kind, focus_id)
);
CREATE INDEX ix_msgconv_user_recent
  ON messenger_conversations (user_id, last_active_at DESC);

-- One row per user. Pointer to "what is this user currently working on".
CREATE TABLE user_focus (
  user_id      BIGINT PRIMARY KEY REFERENCES users(id),
  focus_kind   TEXT   NOT NULL,                   -- 'draft' | 'task' | 'none'
  focus_id     BIGINT,
  set_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at   TIMESTAMPTZ NOT NULL               -- set_at + 24h, bumped each turn
);
```

Notes:

- `focus_kind` is a free-form TEXT column (not a Postgres enum) so v2 can add `freeform`, `po_analysis`, etc. without an enum-alter migration.
- Focus is not keyed on `source`. Switching focus on Slack also takes effect for the same user's Telegram. Each channel still has its own `messenger_conversations` row, so message history is per-medium.
- `messages_json` is capped at the 200 most recent entries when persisting. Oldest entries are silently dropped — no summarisation in v1.

### Focus state machine

States: `none`, `draft`, `task:<id>`.

| From          | To           | Trigger                                                             |
|---------------|--------------|---------------------------------------------------------------------|
| `none`        | `draft`      | Top-level DM with no live focus and no active tasks to pick.        |
| `none`        | `task:N`     | User selects task N from the picker.                                |
| `none`        | `draft`      | User selects "Start something new" in the picker.                   |
| `draft`       | `task:N`     | LLM's `create_task` tool succeeds. Draft conversation row rebound.  |
| `draft`       | `none`       | Focus expires (24h idle). Draft row retained.                       |
| `task:N`      | `task:M`     | User explicitly switches via picker.                                |
| `task:N`      | `none`       | Focus expires OR user clears via picker.                            |
| `task:N`      | `none`       | User types `reset`/`clear` command.                                 |

`expires_at` is bumped by 24h on every turn the router processes for that focus. Terminal task statuses (`done`, `failed`, `cancelled`) do not auto-clear focus — the user may still want to discuss a merged PR — but the picker filters them out of its active list.

### Router flow per inbound message

```
messenger_router.handle(source, user_id, text, thread_ts?)

  1. If thread_ts is set AND maps to a task notification:
       publish human_message(task_id, text, source).
       Do NOT touch user_focus.
       Return.   (existing fast path, preserved)

  2. Load user_focus.
     If expires_at < now() OR focus_kind = 'none':
       focus = none.

  3. If text matches /^(switch|new task|what was i|my tasks|list tasks)\b/i:
       run picker. Return.

  4. Resolve pending picker:
       If a picker is pending in Redis for (source, user_id) AND
       text matches /^\s*(\d+|new)\s*$/i:
         apply pick (set user_focus, ensure conversation row, reply).
         clear pending picker. Return.

  5. If focus is none AND active tasks exist for this user:
       run picker. Return.

  6. If focus is none AND no active tasks:
       focus = draft. Create messenger_conversations row.

  7. Load messenger_conversations row for (user_id, source, focus).
     Append user message.
     Bump user_focus.expires_at = now() + 24h.

  8. Invoke agent.slack_assistant.converse(
         user_id, text, history, home_dir,
         on_create_task=lambda new_task_id: rebind_draft(conv_id, new_task_id),
     )
     Returns (reply_text, appended_messages).

  9. Append assistant + tool turns to row, cap at 200, persist.
     Send reply_text to messenger.
```

### Picker (deterministic, no LLM)

`messenger_router.run_picker(source, user_id)`:

1. Query `tasks WHERE created_by_user_id = u AND status NOT IN ('done', 'failed', 'cancelled')`.
2. Post a message:
   ```
   Which task do you want to pick up?
   1. #42  fix freeform PR rebase  (awaiting_approval)
   2. #57  add /test placeholder route  (in_progress)
   3. Start something new
   Reply with the number or task id.
   ```
3. Record a pending picker in Redis at key `messenger:picker:<source>:<user_id>` → JSON `{tasks:[id,id,...], created_at}` with a 5-minute TTL.
4. Resolution (step 4 in the flow above):
   - Numeric pick: set `user_focus` to `task:N`, ensure a `messenger_conversations` row, reply with a one-line recap of the last assistant turn from that row (or "Picked up task #N — what would you like to do?" if the history is empty).
   - `new`: set `user_focus` to `draft`, create a fresh row, reply "OK, what should we build?".
5. If the user types prose instead of a pick within 5 minutes, the next message falls through the regex check (step 3) and into the normal LLM path. The pending picker is cleared on the first non-matching prose message; the LLM can still infer "let's do 42" via natural language.

### Draft → task rebind

`agent.slack_assistant.converse` accepts an `on_create_task: Callable[[int], Awaitable[None]]` callback. Inside the `create_task` tool dispatcher, after a successful POST to `/tasks`, it awaits the callback with the new task id. The router's callback applies, in one transaction:

```sql
UPDATE messenger_conversations
   SET focus_kind = 'task', focus_id = $N
 WHERE id = $conv_id AND focus_kind = 'draft';

INSERT INTO user_focus (user_id, focus_kind, focus_id, set_at, expires_at)
VALUES ($u, 'task', $N, now(), now() + interval '24 hours')
ON CONFLICT (user_id) DO UPDATE
   SET focus_kind = 'task', focus_id = $N,
       set_at = now(), expires_at = now() + interval '24 hours';
```

Effect: the same Slack DM thread continues seamlessly; the drafting back-and-forth becomes the preamble of task N's conversation history. Opening task N in the web UI later shows the draft messages as the start of its chat.

### Threads (preserved)

Reply-in-thread on a task notification continues to use the existing fast path at `integrations/slack/main.py:413`: it publishes `human_message(task_id, text, source='slack')` directly. The router deliberately does **not** mutate `user_focus` from thread replies — replying to a stale task notification should not yank the user's current DM focus.

### Concurrency

Slack-bolt's event handler is single-flighted per workspace; cross-event races are rare in practice. The router still uses a per-user advisory lock (`pg_advisory_xact_lock(hashtext('msgconv:' || user_id))`) during the read-modify-write of `messenger_conversations.messages_json` to prevent any lost-update edge case. The lock scope is one turn of one user — not a real contention surface.

### Observability

Structured log line on every focus transition:

```
log.info("focus_transition",
         user_id=u, source=s,
         from_focus=f"{from_kind}:{from_id}",
         to_focus=f"{to_kind}:{to_id}",
         reason="expired|picker_pick|draft_rebind|explicit_switch|reset")
```

No new Prometheus counters in v1. Add later if a question can't be answered from logs.

## Migration

- Single Alembic revision: `messenger_conversation_continuity.py` — `CREATE TABLE` for both tables.
- No backfill. In-memory `_sessions` on the running container is lost when v1 deploys. Acceptable.
- `agent/slack_assistant._sessions`, `SESSION_TTL_SECONDS`, `MAX_HISTORY_MESSAGES`, `_get_or_create_session`, `reset_session` are deleted. The router owns lifecycle.
- `converse` signature changes from `(slack_user_id, user_id, text, *, org_id) -> str` to `(user_id, text, history: list[Message], home_dir, *, on_create_task) -> tuple[str, list[Message]]`. `slack_user_id` is no longer needed inside the LLM loop — the router does the resolution upstream.
- `integrations/slack/main.py` calls `messenger_router.handle` for top-level DMs. Threaded replies stay on the existing fast path.

## Testing strategy

Following the CLAUDE.md TDD process. New file `tests/test_messenger_router.py`:

1. `test_first_message_with_no_focus_renders_picker_when_active_tasks_exist`
2. `test_first_message_with_no_focus_and_no_active_tasks_starts_draft`
3. `test_picker_resolves_numeric_pick_sets_focus_and_loads_history`
4. `test_picker_resolves_new_starts_fresh_draft`
5. `test_create_task_rebinds_draft_to_task_and_updates_user_focus`
6. `test_focus_expires_after_24h_idle_triggers_picker_on_next_message`
7. `test_thread_reply_does_not_mutate_user_focus`
8. `test_history_persists_across_simulated_restart` (drop in-process state, recreate router, verify next turn carries prior history)
9. `test_history_capped_at_200_messages`
10. `test_explicit_switch_keyword_triggers_picker_even_with_live_focus`
11. `test_source_isolation_slack_history_independent_of_telegram` (same user, same focus, two sources → two conversation rows)
12. `test_concurrent_messages_for_same_user_serialised`
13. `test_reset_command_clears_focus_but_keeps_conversation_rows`

Existing `tests/test_slack_assistant.py` is updated for the new `converse` signature and the `on_create_task` callback.

Integration test (manual, documented in PR): restart the auto-agent container mid-drafting; confirm the next DM still has the conversation.

## Rollout

1. PR includes migration + router + slack_assistant signature change + test suite, on a single branch.
2. Deploy applies the migration (idempotent on retry).
3. First message per user after deploy lands in the router with `focus = none`; user sees the picker or starts a draft cleanly.
4. Follow-up issue created: "Extend messenger_router to Telegram + add freeform/po_analysis focus kinds (v2)".

## Open questions

None at the time of writing. All identified design dimensions resolved in brainstorming.
