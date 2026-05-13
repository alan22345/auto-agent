# Architect Clarification Flow + Repo-Scoped PO Context — Design

**Date:** 2026-05-13
**Author:** trio follow-up after observing task #167 freeze in `BLOCKED`
**Related:** `2026-05-13-architect-builder-reviewer-design.md` (the trio spec this extends)

---

## Problem

Task #167 ("Parallel universe screen…") was a complex_large freeform task. The trio architect ran its initial pass, produced 8 KB of high-quality design reasoning, and ended with **five open questions for the user** instead of a JSON backlog. The JSON parser failed, the task transitioned to `BLOCKED`, and the trio stalled.

Two underlying gaps:

1. **The architect has no structured "I need answers" path.**
   - `ARCHITECT_INITIAL_SYSTEM` only specifies one valid output: `{"backlog": [...]}`.
   - `ArchitectDecision.action="awaiting_clarification"` exists in the Pydantic type but no prompt teaches the LLM to use it, and no handler routes it.
   - The "FREEFORM MODE AUTONOMY" clause tells the LLM "you cannot ask for human input" but doesn't tell it where to direct questions that genuinely block the design.

2. **The product owner has no repo-scoped "what we want from this repo" context.**
   - `FreeformConfig.po_goal` exists as a short steering goal for the PO analysis loop, but it isn't a comprehensive product brief and isn't reused by other PO entry points.
   - For freeform mode to autonomously answer architect questions, the PO needs the product mission baked into every prompt.

This design closes both gaps in a single coherent flow.

---

## Acceptance criteria

1. A repo has a `product_brief` field (markdown text) describing its mission, requirements, non-goals. The brief is injected into every prompt where the PO is asked to make a product decision.
2. The trio architect can emit an `awaiting_clarification` decision with a single `question` string containing one or more questions.
3. When the architect emits `awaiting_clarification`:
   - The architect's `AgentLoop` session (messages + api_messages) is persisted to a JSON blob on the workspace volume.
   - The parent task transitions `TRIO_EXECUTING → AWAITING_CLARIFICATION`.
   - The `trio_phase` field stays set (`ARCHITECTING` or `ARCHITECT_CHECKPOINT`), which is how the dispatcher disambiguates from the planner's use of `AWAITING_CLARIFICATION`.
4. **Freeform path:** the PO agent is dispatched, given `Repo.product_brief` + the architect's question + recent ARCHITECTURE.md context, returns a `{"answer": "..."}` JSON. The answer is written to the architect_attempts row.
5. **Non-freeform path:** the question is surfaced to the user through the same channels the planner clarification flow already uses (web UI message thread, Slack reply, Telegram reply, Linear comment), keyed by the parent task's source. The user's reply lands as a task_message and is written to the architect_attempts row.
6. Once an answer is written, the parent transitions `AWAITING_CLARIFICATION → TRIO_EXECUTING`, the architect's saved AgentLoop session is reloaded, the answer is injected as a synthetic user message, and the AgentLoop continues running until it emits a backlog (or another awaiting_clarification, capped at 3 rounds).
7. Q&A pairs are visible in the existing Architect panel on the task detail page in `web-next`.
8. Container restart in `AWAITING_CLARIFICATION` state is recoverable: the recovery loop does NOT auto-resume (still waiting on an answer); but it does re-fire the resume event if an answer was already written before the crash.

---

## Non-goals

- Re-architecting the planner's clarification flow. Untouched.
- Per-question answer UI. v1 accepts a single combined answer per clarification round.
- Auto-approve PO answers / human-in-the-loop oversight of PO answers.
- Backfilling `product_brief` for existing repos. The column is nullable; PO works without it (with a warn log).
- Adopting `product_brief` in the `po_analyzer` suggestion-generation prompts. Follow-up if it proves useful.
- A `request_po_decision` synchronous tool for the architect. We chose async-via-state-machine for symmetry with the user path; the sync alternative was considered and rejected during brainstorming.

---

## Architecture

```
┌─── Architect emits awaiting_clarification ───┐
│                                              │
│  AgentLoop pauses; Session JSON saved to     │
│  workspace dir. ArchitectAttempt row stores  │
│  clarification_question + cycle.             │
│                                              │
└───────────────┬──────────────────────────────┘
                ▼
   transition: TRIO_EXECUTING → AWAITING_CLARIFICATION
   trio_phase stays set
                │
        ┌───────┴─────────┐
        │                 │
   freeform_mode?       not freeform
        │                 │
        ▼                 ▼
   po_agent.answer    publish task.clarification_needed
   (product_brief     (existing event; per-integration
   injected)          formatters in integrations/* already
                      surface it, keyed by task.source)
        │                 │
        └────────┬────────┘
                 ▼
   answer lands as a task_message + clarification_answer
   on the architect_attempts row
                 │
                 ▼
   transition: AWAITING_CLARIFICATION → TRIO_EXECUTING
                 │
                 ▼
   architect.resume(): reload AgentLoop session,
   inject answer as user message, continue.
```

After the answer lands as a task_message, the two paths are byte-identical: same DB update, same event, same resume code. The only divergence is **who produces the answer**.

---

## Data model

### Migration 034

**`repos`**

```python
product_brief = Column(Text, nullable=True)
```

Free-text markdown. Describes the product mission, requirements, non-goals for the repo. Injected into every PO prompt where the PO is asked to make a product-shaped decision. Nullable so existing repos don't need backfill; PO logs a `warn` when invoked against a freeform repo that has it null.

**`architect_attempts`**

```python
clarification_question = Column(Text, nullable=True)
clarification_answer   = Column(Text, nullable=True)
clarification_source   = Column(String(16), nullable=True)  # 'user' | 'po'
session_blob_path      = Column(String(512), nullable=True)
```

- `clarification_question` — the prose the architect emitted in `ArchitectDecision.question`.
- `clarification_answer` — the prose the answerer produced.
- `clarification_source` — for audit (and for the UI badge).
- `session_blob_path` — relative path under the workspace tree where `Session.save()` wrote the JSON. Loaded by `architect.resume()`.

A clarification attempt is **one** `architect_attempts` row. When the architect emits `awaiting_clarification`, the row is inserted with the question populated and the answer/source/cycle filled in later. The Pydantic `ArchitectAttemptOut` gains three of the four new fields — `clarification_question`, `clarification_answer`, `clarification_source`. The `session_blob_path` stays internal (not exposed to the UI).

### State machine

`orchestrator/state_machine.py::TRANSITIONS` (additions only):

```python
TaskStatus.TRIO_EXECUTING: {
    TaskStatus.PR_CREATED,
    TaskStatus.BLOCKED,
    TaskStatus.AWAITING_CLARIFICATION,   # NEW
},
TaskStatus.AWAITING_CLARIFICATION: {
    TaskStatus.PLANNING,
    TaskStatus.CODING,
    TaskStatus.FAILED,
    TaskStatus.TRIO_EXECUTING,           # NEW
},
```

The disambiguation rule lives in one place — the dispatcher seam. When `task.status == AWAITING_CLARIFICATION`, check `task.trio_phase`:

| `trio_phase` value             | Origin                  | Resume target      | Handler                                  |
|--------------------------------|-------------------------|--------------------|------------------------------------------|
| `None`                         | planner                 | `PLANNING`         | existing `handle_clarification_response` |
| `ARCHITECTING`                 | architect initial pass  | `TRIO_EXECUTING`   | new `architect.resume()`                 |
| `ARCHITECT_CHECKPOINT`         | architect checkpoint    | `TRIO_EXECUTING`   | new `architect.resume()`                 |

### Events

One new event type:

```python
class TaskEventType(StrEnum):
    ...
    ARCHITECT_CLARIFICATION_NEEDED   = "task.architect_clarification_needed"
    ARCHITECT_CLARIFICATION_RESOLVED = "task.architect_clarification_resolved"
```

`*_NEEDED` is published when the architect emits the decision; the dispatcher hears it and either kicks off PO or notifies the user. `*_RESOLVED` is published when an answer is written; another handler hears it, transitions state, and calls `architect.resume()`.

---

## Components

### 1. `architect.py` — emit + resume

`_handle_decision` (renamed from inline JSON handling) recognises `action == "awaiting_clarification"`:

```python
if decision.action == "awaiting_clarification":
    # 1. Persist the AgentLoop session.
    session_path = f"{workspace.root}/.trio-session.json"
    session = Session(session_id=f"trio-{parent_id}", storage_dir=workspace.root)
    await session.save(agent.messages, agent.api_messages)

    # 2. Write attempt row.
    cycle = _next_cycle(s, parent_id, ArchitectPhase.INITIAL)
    s.add(ArchitectAttempt(
        task_id=parent_id,
        phase=ArchitectPhase.INITIAL,
        cycle=cycle,
        reasoning=output,
        decision={"action": "awaiting_clarification"},
        clarification_question=decision.question,
        session_blob_path=".trio-session.json",
    ))

    # 3. Transition state and publish event.
    await transition(s, parent, TaskStatus.AWAITING_CLARIFICATION, "Architect needs answers")
    await s.commit()
    await publish(Event(
        type=TaskEventType.ARCHITECT_CLARIFICATION_NEEDED,
        task_id=parent_id,
        payload={"question": decision.question},
    ))
    return  # the trio loop exits cleanly
```

New function `architect.resume(parent_task_id: int)`:

```python
async def resume(parent_task_id: int) -> None:
    # Load the latest architect_attempts row with clarification_answer set.
    # Reconstruct workspace path.
    # Build a new AgentLoop with Session(session_id="trio-<parent_id>",
    #   storage_dir=workspace.root); on first run() it auto-loads the
    #   serialized messages.
    # Inject answer as a synthetic user message:
    #   "ANSWER FROM {PRODUCT OWNER|USER}:\n\n{answer}\n\n
    #    Now produce the backlog JSON."
    # Run the AgentLoop again from that point.
    # On a backlog → continue the existing run_initial post-LLM flow
    #   (commit scaffolds, open initial PR).
    # On another awaiting_clarification → loop guard kicks in (see below).
```

Loop guard: `architect_attempts` rows with `clarification_question IS NOT NULL` for this parent are counted. If `>= TRIO_MAX_CLARIFICATIONS` (default 3) and the architect asks again, transition `TRIO_EXECUTING → BLOCKED` instead of looping further.

If `session_blob_path` is missing or unreadable on resume, log `trio.architect.session_lost` and fall back to `architect.run_initial` with the original task description and an appended `## Prior clarification` section containing the Q&A.

### 2. `agent/po_agent.py` — new module

Single entry point:

```python
async def answer_architect_question(parent_task_id: int) -> None:
    """Run the PO to answer the architect's outstanding clarification.

    Reads the latest architect_attempts row for parent_task_id where
    clarification_question IS NOT NULL AND clarification_answer IS NULL.
    Loads Repo.product_brief. Builds a readonly agent. Runs it.
    Writes the answer (or the failure note) to the same row and
    publishes ARCHITECT_CLARIFICATION_RESOLVED.
    """
```

Prompt structure:

```
<product_brief from Repo, if not null>

<recent ARCHITECTURE.md from the trio integration branch, if present>

You are the Product Owner. The architect has paused and asked the
following question. Answer as the PO, grounded in the product brief
above.

Question:
<architect's clarification_question>

Output ONLY a JSON object on its own lines:
{"answer": "<your answer, max 1500 chars>"}
```

Tools available: readonly file tools (`file_read`, `glob_tool`, `grep_tool`) — let the PO browse code to ground answers when useful. No write tools. `max_turns=8`.

On parse failure / exception: write the error message as the answer with `clarification_source='po'`, publish `*_RESOLVED` so the architect resumes and decides what to do (likely retry or pick a default). Three consecutive PO failures → architect's resume produces another `awaiting_clarification` and the 3-round cap blocks the parent.

### 3. `run.py` — dispatcher and resume seam

```python
async def on_architect_clarification_needed(event: Event) -> None:
    """Route the architect's question to PO (freeform) or to the user."""
    async with async_session() as s:
        task = await get_task(s, event.task_id)
        if not task or task.status != TaskStatus.AWAITING_CLARIFICATION:
            return
        if task.freeform_mode:
            from agent.po_agent import answer_architect_question
            asyncio.create_task(answer_architect_question(task.id))
        else:
            # Reuse the planner's clarification event; existing
            # integrations (telegram/slack/linear) surface it.
            await publish(Event(
                type=TaskEventType.CLARIFICATION_NEEDED,
                task_id=task.id,
                payload={"question": event.payload["question"],
                         "phase": "trio_architect"},
            ))


async def on_architect_clarification_resolved(event: Event) -> None:
    """Answer landed; resume the architect."""
    async with async_session() as s:
        task = await get_task(s, event.task_id)
        if not task or task.status != TaskStatus.AWAITING_CLARIFICATION:
            return
        await transition(s, task, TaskStatus.TRIO_EXECUTING, "Architect resuming after clarification")
        await s.commit()
        from agent.lifecycle.trio import architect
        asyncio.create_task(architect.resume(task.id))


bus.on(TaskEventType.ARCHITECT_CLARIFICATION_NEEDED, on_architect_clarification_needed)
bus.on(TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED, on_architect_clarification_resolved)
```

### 4. Inbound message dispatcher

The existing web/Slack/Telegram/Linear adapters that post inbound messages as `task_messages` currently call `handle_clarification_response` for planner clarifications. They get a small fork:

```python
async def handle_clarification_inbound(task_id: int, content: str) -> None:
    async with async_session() as s:
        task = await get_task(s, task_id)
        if task.status != TaskStatus.AWAITING_CLARIFICATION:
            return  # nothing to do
        if task.trio_phase is not None:
            # Trio architect is waiting. Write answer + publish.
            attempt = (await s.execute(
                select(ArchitectAttempt)
                .where(ArchitectAttempt.task_id == task_id)
                .where(ArchitectAttempt.clarification_question.is_not(None))
                .where(ArchitectAttempt.clarification_answer.is_(None))
                .order_by(ArchitectAttempt.created_at.desc())
                .limit(1)
            )).scalar_one_or_none()
            if attempt is None:
                return
            attempt.clarification_answer = content
            attempt.clarification_source = 'user'
            await s.commit()
            await publish(Event(
                type=TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED,
                task_id=task_id,
            ))
        else:
            # Planner. Existing path.
            await handle_clarification_response(task_id, content)
```

### 5. UI — Architect panel

`web-next/components/trio/ArchitectAttemptsPanel.tsx` already renders one row per attempt. Add to the row's expanded view:

- If `clarification_question` is set → render a "Question" subsection (markdown).
- If `clarification_answer` is set → render an "Answer" subsection with a small badge (`PO` / `User`) tied to `clarification_source`.

`ArchitectAttemptOut` gains the four new fields (regenerated TS types).

Settings page for the repo gains a `product_brief` markdown textarea (under the existing freeform config block, but visible for all repos).

### 6. Recovery on container restart

`agent/lifecycle/trio/recovery.py::resume_all_trio_parents` currently picks up tasks in `TRIO_EXECUTING`. Add a parallel branch for tasks in `AWAITING_CLARIFICATION` with `trio_phase IS NOT NULL`:

```python
# AWAITING_CLARIFICATION with trio_phase set — could be either still
# waiting for an answer (do nothing) or the answer arrived just before
# the crash (re-publish RESOLVED so resume runs).
for task in awaiting_trio:
    latest = ... # most recent ArchitectAttempt row with question set
    if latest.clarification_answer is not None:
        await publish(Event(
            type=TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED,
            task_id=task.id,
        ))
```

Idempotent because the resume handler re-checks state.

---

## Prompt changes

### `ARCHITECT_INITIAL_SYSTEM`

The current "FREEFORM MODE AUTONOMY" paragraph is replaced with a softer formulation that exposes the new schema in both modes:

```
You have a Product Owner you can consult when a product-shaped decision
genuinely blocks the design. Use this only when (a) the answer
materially changes the architecture, AND (b) you cannot reasonably
default to one branch and ship.

To ask the PO (or, when this repo is not in freeform mode, the human
user), emit your reasoning followed by a JSON object on its own lines:

```json
{"decision": {"action": "awaiting_clarification", "question": "..."}}
```

The `question` is a single string. Pack multiple sub-questions into it
as a markdown-numbered list with the reason each matters.

DO NOT use this for: choices you could make and revise later, missing
data you could grep for yourself, or to dodge committing to a stack.
The trio is permissive — defaulting and shipping is almost always
better than blocking.
```

### `ARCHITECT_CHECKPOINT_SYSTEM`

Same addition. The checkpoint already has a `decision.action` field for `continue|revise|done`; add `awaiting_clarification` as a fourth option with the same schema and the same constraints.

---

## Testing

| File | What it tests |
|------|---------------|
| `tests/test_trio_models_migration.py` | (extend) the 4 new columns exist; the 1 new repos column exists. |
| `tests/test_architect_emit_clarification.py` | (new) `_handle_decision` recognises `awaiting_clarification`, persists session, transitions state, publishes `*_NEEDED`. |
| `tests/test_po_agent_answer.py` | (new) `answer_architect_question` happy path with mocked LLM; malformed-JSON fallback; missing-product_brief warn. |
| `tests/test_clarification_dispatcher.py` | (new) `handle_clarification_inbound` with trio_phase set vs not set; verifies dual-routing without crosstalk. |
| `tests/test_architect_resume.py` | (new) `architect.resume` loads session, injects answer, runs AgentLoop with mocked provider; emits backlog. Includes session-lost fallback test. |
| `tests/test_trio_state_machine.py` | (extend) the two new transitions are allowed; planner `AWAITING_CLARIFICATION → TRIO_EXECUTING` would be valid in the table but the dispatcher refuses it when `trio_phase IS NULL` (defensive). |
| `tests/test_trio_clarification_loop_guard.py` | (new) simulate 4 consecutive awaiting_clarification cycles, assert transition to BLOCKED on the 4th. |
| `tests/test_trio_clarification_e2e_mocked.py` | (new) end-to-end: emit clarification → freeform dispatcher → PO returns canned answer → resume → backlog populated. Mocks the LLM at the providers, not at our seams. |
| `tests/test_trio_recovery_clarification.py` | (new) recovery loop re-publishes RESOLVED for tasks whose answer landed pre-crash. |

---

## Open questions resolved during brainstorming

| Q | Decision |
|---|----------|
| Where does the repo PO context live? | `Repo.product_brief` (new Text column on `repos`). |
| Question schema | Single `question: str` (reuses existing `ArchitectDecision.question`). |
| Sync vs async PO | Async via state machine. |
| Architect resume strategy | Same AgentLoop session resumed turn-by-turn; existing `agent.session.Session` class used for persistence. |
| State machine integration | Reuse `AWAITING_CLARIFICATION`, disambiguate by `trio_phase`. |
| Q&A visibility | Persist on `architect_attempts`, show in the Architect panel. |

---

## File map

```
shared/models/core.py            # Repo.product_brief
shared/models/trio.py            # ArchitectAttempt.{clarification_question,
                                 #   clarification_answer, clarification_source,
                                 #   session_blob_path}
shared/types.py                  # ArchitectAttemptOut gains 4 fields;
                                 # RepoData gains product_brief
migrations/versions/034_*.py     # NEW — adds the 5 columns

orchestrator/state_machine.py    # +2 transitions
orchestrator/router.py           # PATCH /repos/{id}/product-brief
                                 # extend PATCH /tasks/{id}/messages flow
                                 # (or keep existing endpoint, route in
                                 # handle_clarification_inbound)

agent/lifecycle/trio/
  architect.py                   # _handle_decision branch + resume()
  prompts.py                     # ARCHITECT_INITIAL_SYSTEM + ARCHITECT_CHECKPOINT_SYSTEM
                                 #   gain the awaiting_clarification spec
  recovery.py                    # AWAITING_CLARIFICATION recovery branch

agent/po_agent.py                # NEW — answer_architect_question
                                 # (no new code in shared/notifier.py —
                                 # we reuse task.clarification_needed
                                 # event which existing integrations
                                 # already format and surface)

run.py                           # on_architect_clarification_needed,
                                 # on_architect_clarification_resolved,
                                 # bus.on(...) wiring,
                                 # handle_clarification_inbound seam

web-next/types/api.ts            # regen
web-next/components/trio/
  ArchitectAttemptsPanel.tsx     # render Q + A subsections
web-next/components/repos/
  RepoSettingsForm.tsx           # product_brief textarea

tests/                           # 8 new test files (see Testing section)
```

---

## Rollout

1. Land migration 034 + model changes + Pydantic type additions. Ship to VM.
2. Land architect emit + resume + prompts. Behind a `TRIO_CLARIFICATION_ENABLED` env flag (default off). Manual test: queue a task whose description is intentionally ambiguous, confirm the architect emits `awaiting_clarification`.
3. Land PO agent + dispatcher + inbound seam. Manual test (freeform repo): same task as step 2, watch PO answer get written.
4. Land UI changes + product_brief settings form.
5. Flip `TRIO_CLARIFICATION_ENABLED=true` on the VM. Validate against task #167 (which is currently BLOCKED) by transitioning it back to TRIO_EXECUTING and letting the new architect prompt run.
6. Remove the env flag in a follow-up commit once it's been stable for a week.
