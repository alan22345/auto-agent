# [ADR-017] Trio iteration phase — post-PR feedback loop

## Status

Accepted

## Context

Today, when the trio dispatcher opens an integration PR for a `complex_large`
task, the task lands in `PR_CREATED` and stays there. Any user feedback posted
after that point — whether via the web-next chat, a Slack DM, a Telegram
reply, or a GitHub PR comment — has nowhere to go:

- The web-next `POST /tasks/{id}/message` (singular) endpoint only writes a
  row to `task_history` and never publishes an event.
- The Slack/Telegram thread-reply path publishes `task.feedback`, which is
  only routed to `handle_clarification_inbound` and gated on
  `AWAITING_CLARIFICATION`. PR-time feedback is silently dropped.
- The GitHub PR comment poller's `human.message` event is routed by
  `route_human_message`, which has a `pr_created` branch — but that branch
  calls `handle_pr_review_comments`, designed for the non-trio coding flow.
  It does not re-enter the trio dispatcher.

Concrete repro on 2026-05-15: task 5 reached `PR_CREATED` (PR #46). The user
posted "with the graph showing the average wellbeing can you break it down
further…" via the web UI. The message landed in `task_history` and the agent
did nothing.

The user has stated: *"we need to bake in a phase that reacts to user
feedback so that the user can iterate on a task as much as they want."*

Constraints from the brainstorming session:

1. **Scope.** Iteration is only available *between PR-open and merge* — not
   on terminal `DONE`/`FAILED` tasks, not during pre-PR phases (planning
   and the design gate already have their own conversation paths).
2. **Shape.** The full trio re-iterates: a new architect pass converts the
   feedback into a fresh backlog; the per-item builder loop runs; the final
   reviewer + smoke agent runs; the existing PR picks up the new commits
   via a normal additive `git push`.
3. **Channels.** Web UI chat, Slack DM, Telegram reply, and GitHub PR
   comments must all trigger the same iteration path. The user must not
   have to remember which channel "works".
4. **Termination.** Only the GitHub PR merge webhook moves the task to
   `DONE`. No idle timeout, no in-band "ship it" command. The iteration
   loop is open-ended until merge.
5. **No force-push.** The existing dispatcher already pushes each builder
   commit additively to the integration branch. Iteration cycles do the
   same. No history is rewritten.

## Decision

Add a new long-lived `AWAITING_REVIEW` phase and an `ITERATING` sub-state.
`PR_CREATED` stops being a long-lived status and becomes a one-shot
transit event marking the moment the integration PR is first opened.

### State machine

| From | Trigger | To |
|---|---|---|
| `FINAL_REVIEW` | smoke passes + integration PR opened | `PR_CREATED` |
| `PR_CREATED` | (auto-fall-through, same handler) | `AWAITING_REVIEW` |
| `AWAITING_REVIEW` | `human.message` event for this task | `ITERATING` |
| `ITERATING` | iteration loop completes | `AWAITING_REVIEW` |
| `AWAITING_REVIEW` | GitHub PR merge webhook | `DONE` |
| `ITERATING` | unrecoverable architect/dispatch failure | `BLOCKED` |

`PR_CREATED` keeps its place as a mandatory single-fire transit point so
the existing notifier ("🎉 Integration PR opened") and any consumer keyed
off `task.pr_created` event are unaffected. The fall-through to
`AWAITING_REVIEW` happens in the same handler that opens the PR.

A new `TrioPhase.ARCHITECT_ITERATING` is added so the UI can distinguish
"reacting to your feedback" from initial design / build / checkpoint.

### Channel adapters — every channel emits `human.message`

| Channel | Today | Change |
|---|---|---|
| Web UI chat (`POST /tasks/{id}/message`) | writes `task_history` only | also `publish(human_message(task_id, message, source="web"))` |
| Slack DM (thread reply) | publishes `task.feedback` → `handle_feedback_event` (clarification only) | broaden `handle_feedback_event` so when status ∈ {`AWAITING_REVIEW`, `ITERATING`} it re-emits `human.message` |
| Telegram reply-to-message | same path as Slack | same fix |
| GitHub PR comments | poller already publishes `human.message` | verify it fires for trio tasks (likely already does — checks `task.pr_url`) |

The web UI's WebSocket-driven path already publishes `human.message`
(`web/main.py:451`). The fix here is for the *singular* HTTP endpoint that
the legacy web UI uses for the chat panel.

### Routing — `route_human_message` extension

`agent/lifecycle/conversation.py::route_human_message` already switches on
`task.status`. Two new branches are added:

```python
elif task.status in (TaskStatus.AWAITING_REVIEW, TaskStatus.ITERATING) and _is_trio(task):
    await iteration.handle_iteration_feedback(task_id, message)
elif task.status == TaskStatus.PR_CREATED and not _is_trio(task):
    # Existing non-trio coding flow — unchanged.
    await review.handle_pr_review_comments(task_id, message)
```

`_is_trio(task)` is `task.complexity == TaskComplexity.COMPLEX_LARGE` —
that's the discriminator the dispatcher itself uses to decide whether to
run the trio flow, and it stays stable across the task's lifetime.

While a task is `ITERATING`, additional feedback messages get queued via
`task_channel(task_id).push_guidance(message)` so the running architect
or builder picks them up between turns. Module-level
`_active_iteration_tasks` set guards against re-entrant feedback,
mirroring the existing `_active_clarification_tasks` pattern.

### New module — `agent/lifecycle/trio/iteration.py`

```python
async def handle_iteration_feedback(task_id: int, message: str) -> None:
    """Entry point for user feedback on a trio task in AWAITING_REVIEW.

    Refetches the task and bails if status is already DONE (merge
    webhook beat us). Otherwise transitions AWAITING_REVIEW → ITERATING,
    builds iteration_context, and re-enters run_trio_parent.
    """
```

### New architect entrypoint — `architect.iterate(task_id, iteration_context)`

Sibling to `architect.run_initial` and `architect.checkpoint`. Pinned
context delivered to the architect call:

- The original task description
- `.auto-agent/design.md`
- The user's feedback message
- `git diff <base>...<integration_branch>` — full PR diff
- The existing `trio_backlog` (so the architect sees what's already shipped)

System-prompt addendum: *"The user has reviewed the PR and given the
feedback below. Emit a fresh backlog of items needed to address it. You
may also re-open already-shipped items by including them with
`status=pending` if the feedback requires re-doing earlier work."*

Uses the existing `submit-backlog` skill — same decision protocol as
`run_initial`. The architect's new items are **appended** to the
existing `trio_backlog` (not replaced) so the audit trail of what was
shipped initially survives. The per-item dispatcher loop already
processes only items with `status="pending"`, so appended pending items
are picked up naturally and previously-`done` items are left alone.
Items that need re-doing land as new pending items with a fresh ID
(e.g. `S2.r1`, `S2.r2` for successive iterations of S2) — the
architect chooses the suffix.

### Dispatcher extension — `run_trio_parent(parent, iteration_context=None)`

New keyword argument, parallel to the existing `repair_context`:

```python
async def run_trio_parent(
    parent: Task,
    *,
    repair_context: dict | None = None,
    iteration_context: dict | None = None,
) -> None:
    if iteration_context is not None:
        await _set_trio_phase(parent.id, TrioPhase.ARCHITECT_ITERATING)
        await architect.iterate(parent.id, iteration_context=iteration_context)
    elif repair_context is not None:
        await _set_trio_phase(parent.id, TrioPhase.ARCHITECT_CHECKPOINT)
        await architect.checkpoint(parent.id, repair_context=repair_context)
    else:
        gate_ok = await _advance_through_design_gate(parent)
        if not gate_ok:
            return
    ...  # existing per-item loop unchanged
```

After the per-item loop finishes in iteration mode (`iteration_context
is not None`), the dispatcher transitions the task from `ITERATING` →
`AWAITING_REVIEW` and publishes a new `task.iteration_complete` event
("✅ updated PR with your changes"). The final reviewer + smoke agent
still run as part of the per-item loop's tail. No second PR is opened;
the existing PR auto-picks-up the new commits because the head branch
moved.

### Edge cases

- **Merge during iteration.** GitHub merge webhook fires while iteration
  is running. The existing webhook handler transitions the task to
  `DONE`. The iteration loop's per-iteration status read (already done
  in the existing per-item loop's `if p.status == BLOCKED: return`
  pattern) detects this and exits without re-transitioning.
  `handle_iteration_feedback` refetches and bails early if the task is
  already `DONE` by the time it tries to transition.
- **Concurrent feedback.** Second `human.message` arrives while
  `ITERATING`. Routed to `push_guidance` on the task channel. The
  architect's `iterate` call sees both messages on its next turn (it
  drains guidance the same way the existing per-item builder does).
- **Architect produces empty backlog.** Treat as a no-op iteration:
  transition back to `AWAITING_REVIEW`, publish
  `task.iteration_complete` with `summary="no code change needed"`.
- **Iteration introduces a regression.** Caught by the existing
  smoke agent inside the per-item loop; gap-fix dispatches a builder to
  address it. No new code path.
- **Branch has been merged and deleted before iteration completes.**
  `_ensure_integration_branch_checked_out` fails. The dispatcher's
  existing failure path transitions to `BLOCKED`. The user can re-create
  the task if they want to continue working on it; the merged code is
  already in main.

### Schema impact

- Migration: `AWAITING_REVIEW` already exists in the `taskstatus`
  enum (verified by enum dump on the live DB 2026-05-15) — this ADR
  promotes it from "rare/legacy" to "load-bearing", no schema change
  for it. The migration adds `'ITERATING'` to `taskstatus` and
  `'ARCHITECT_ITERATING'` to `triophase`. `ALTER TYPE … ADD VALUE IF
  NOT EXISTS …` so the migration is idempotent.
- One new event factory in `shared/events.py`:
  `task_iteration_complete(task_id, summary)` → `TaskEventType.ITERATION_COMPLETE`,
  consumed by the Slack/Telegram notifier and web-next.
- No new tables. `task_history` picks up new transition pairs but no
  schema change.
- `web-next` UI: render `AWAITING_REVIEW` distinctly from
  `PR_CREATED` (currently both probably show "PR opened"); render
  `ITERATING` with a spinner + "responding to your feedback".

### Audit of existing `PR_CREATED` consumers

Before shipping, audit any code that treats `PR_CREATED` as long-lived:

- `route_human_message`'s status switch (the existing `pr_created`
  branch becomes the non-trio fallback per the routing change above).
- The GitHub merge-webhook handler: must accept `AWAITING_REVIEW` and
  `ITERATING` as valid pre-merge states, not just `PR_CREATED`.
- The CI poller's status guard.
- Slack/Telegram notifiers — `_fmt_task_pr_created` stays as the
  PR-open notifier; new `_fmt_task_iteration_complete` is added.
- Anywhere the UI broadcasts task status (web-next colour, lists, etc.).

Sites currently checking `task.status == PR_CREATED` for "PR is open"
semantics must become `task.status in (PR_CREATED, AWAITING_REVIEW,
ITERATING)` or just `AWAITING_REVIEW` depending on intent.

## Consequences

### Easier

- **Closed-loop iteration UX.** User feedback on a PR loops the trio
  pipeline without any new prompting or context-juggling on the user's
  side. They post in any channel, the agent works, the PR updates.
- **Consistent across channels.** Web UI, Slack, Telegram, and GitHub
  all share one routing target (`iteration.handle_iteration_feedback`),
  so adding or removing channels later is a pure adapter change.
- **`AWAITING_REVIEW` is now a useful first-class state.** It's the
  state every team has wanted to filter on ("show me PRs the agent
  isn't actively working on") and was previously conflated with
  `PR_CREATED`.

### Harder

- **State-machine surface widens by two states.** `ITERATING` is new,
  and `AWAITING_REVIEW` changes meaning. Any consumer of task status
  needs an audit pass. The migration's "find me everyone checking
  `PR_CREATED`" grep is mandatory work, not optional polish.
- **Architect prompts diverge.** `run_initial`, `checkpoint`, and now
  `iterate` are three architect entrypoints with three different system
  prompts. If they drift, the user experience drifts. We'll need a
  shared base prompt + per-entrypoint addendum (the current
  `ARCHITECT_INITIAL_SYSTEM` already hints at this shape).
- **Iteration is expensive.** A full trio re-iteration takes hours.
  Users posting "can you also do X" carelessly will spend real Bedrock
  budget. The design accepts this — the user explicitly chose "full
  trio re-iteration" over a lightweight coder pass — but the eventual
  classifier (light/medium/heavy from the brainstorming menu) is a
  natural follow-on if this becomes painful.

### Out of scope (explicitly)

- A lightweight "single-coder pass" mode. Considered, deferred.
- A user-driven "ship it" / "lgtm" command that fast-tracks merge.
  Considered, deferred — merge stays a GitHub-side action.
- Idle timeout that auto-transitions abandoned `AWAITING_REVIEW` tasks
  to `DONE` after N days. Considered, deferred. Easy to add later if
  abandoned PRs become a problem.
- Non-trio (simple/complex) feedback iteration. Only `complex_large`
  trio tasks get this phase. The non-trio coding flow already has
  `handle_pr_review_comments` for PR-comment-driven iteration.
