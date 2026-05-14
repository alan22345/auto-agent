# [ADR-013] Trio drives its backlog via subagents, not child task rows

## Status

Proposed

## Context

The trio lifecycle (architect → coder → reviewer) was implemented by
materialising every backlog item the architect produced as a fresh
top-level row in the `tasks` table:

```python
# agent/lifecycle/trio/scheduler.py::dispatch_next
child = Task(
    title=item.get("title") or item.get("description") or "(trio subtask)",
    ...
    status=TaskStatus.QUEUED,
    complexity=TaskComplexity.COMPLEX,
    parent_task_id=refreshed_parent.id,
)
session.add(child)
await session.commit()
await publish(task_created(child.id))
```

The orchestrator then treats those child rows as ordinary tasks: they
compete for concurrency slots, go through `on_task_created` →
`on_task_classified` → the queue → coding → review → PR, and end up
with their own branch, PR, and CI run.

Three load-bearing problems this design creates, each enough on its
own:

1. **Concurrency-cap deadlock.** The system enforces a per-org cap of
   two concurrent active tasks (see `orchestrator/queue.py` +
   `architecture_decisions` memory). A trio parent in
   `TRIO_EXECUTING/AWAITING_BUILDER` already occupies one slot. Its
   children, written at `status=QUEUED`, can only run when a slot is
   free. If the parent emits more than one child, or another unrelated
   task is also active, the children sit in `QUEUED` indefinitely —
   and the parent's `await_child` loop sits waiting on them. Nothing
   can move. Task 170 hit this on 2026-05-14: parent
   `TRIO_EXECUTING/AWAITING_BUILDER`, child 171 `QUEUED`, neither
   progressing.

2. **State-machine mismatch.** The trio scheduler writes
   `status=QUEUED` directly, then publishes `task.created`. The
   orchestrator's `on_task_created` handler then calls
   `transition(session, task, TaskStatus.CLASSIFYING, ...)` — but
   `QUEUED → CLASSIFYING` is not in `TRANSITIONS`, so the dispatcher
   raises `InvalidTransition`. Today the exception is swallowed by the
   `event_processing_error` log line in `run.py::orchestrator_loop`
   and the message is `ack`'d anyway, so we lose the event silently.
   This is observable in the logs as the only line between
   `trio.scheduler.dispatched` and the parent stalling.

3. **Wrong unit of concurrency.** Each backlog item gets its own repo
   clone, its own agent loop, its own session, its own PR, its own CI
   run. The user thinks of "build a parallel-universe screen" as ONE
   logical request; the system bills it as N+1 separate tasks against
   the org's quota. Even ignoring the deadlock, this conflates
   "execution slot" (a coarse-grained user-facing budget) with
   "work item" (an internal phase the architect chose to factor out).

The `subagent` tool already exists at `agent/tools/subagent.py` —
introduced via the vendored mattpocock skills (ADR-003) and renamed
from "Agent" to "subagent" at vendor time. It dispatches a fresh
`AgentLoop` with the full tool registry minus itself, sharing the
caller's workspace, with a 10-minute hard timeout per invocation.
The skill `superpowers:subagent-driven-development` documents this as
the right pattern for "independent tasks within the current session".

## Decision

Trio drives its backlog from inside the parent task's slot, using
the `subagent` tool, with no new `Task` rows and no Redis-stream
round-trips per backlog item.

**Flow:**

1. Architect runs `run_initial` as today, produces the backlog JSON
   on the parent task row (`tasks.trio_backlog`).
2. `run_trio_parent` reads the backlog in a loop. For each pending
   item, it runs a coder↔reviewer dialogue **inside the parent's
   slot**, with the architect (= the parent's main agent loop)
   holding the cross-item context. No new `Task` row, no
   `publish(task_created(...))`, no Redis-stream round-trip.

   Per backlog item:

   a. **Coder subagent (fresh context, full tool registry).** Gets
      the work item title/description, the workspace path, the
      current branch state, and the architecture-pass summary it
      needs to fit into. Makes the change. Returns its output +
      tool-call log + a short "what I did and why" rationale.

   b. **Reviewer subagent (fresh context, readonly tools + UI
      capture).** Gets the diff the coder just produced, the
      original work item spec, and the coder's rationale. Returns
      `{ok: bool, feedback: str}`. The reviewer's tool registry is
      restricted to `file_read`, `grep`, `glob`, `git` (diff/log
      only), and `browse_url` — so it can inspect the code and
      visually verify UI changes against the spec, but cannot edit
      files or run side-effectful commands.

   c. **Coder↔reviewer dialogue, up to 3 round-trips.** If the
      reviewer rejects, the result + feedback go back to the SAME
      coder (resume=True) so it can respond — either fix the issue
      or disagree, with reasoning. The coder is explicitly allowed
      to push back: e.g. "the UI looks half-baked because the
      backlog item only asked for the data plumbing — the polish is
      a later item". The reviewer then either accepts the
      explanation (returns `ok=true`) or re-rejects with sharpened
      feedback. Cap: 3 coder→reviewer exchanges per item.

   d. **Architect tiebreaker.** If the coder and reviewer haven't
      converged after 3 round-trips, the dialogue transcript goes
      back to the architect (the parent's main agent loop, which
      holds the full task context). The architect decides:
      - "coder is right, mark item done" → backlog item
        `status="done"`, continue.
      - "reviewer is right, dispatch the coder again with this
        specific guidance" → fresh coder subagent with the
        architect's instructions appended, fresh review cycle.
      - "the item itself is wrong, revise the backlog" → architect
        edits `tasks.trio_backlog` (split, merge, reword the item),
        re-dispatches.
      - "we can't get past this without human input" → emit a
        clarification via `_emit_clarification` (existing path) and
        pause the parent in `AWAITING_CLARIFICATION`.

3. Architect checkpoint runs as today against the parent's
   accumulated changes after the backlog is drained; can revise the
   backlog if needed (which re-enters the per-item loop).
4. When backlog is drained the parent opens the final integration PR
   and transitions to `PR_CREATED`. Unchanged from today.

**What gets deleted:**

- The `child = Task(...)` construction in
  `agent/lifecycle/trio/scheduler.py::dispatch_next` plus the
  `await publish(task_created(child.id))` that goes with it.
- `scheduler.await_child(parent, child)` — there is no separate Task
  to await; subagent calls are `await`ed inline.
- The existing `TRIO_REVIEW` status path for child tasks — replaced
  by the in-process reviewer subagent. Status enum stays for now
  (other features may key on it) but ceases to be written.
- The `parent_task_id` consumer paths for trio. (The column stays —
  it predates trio and may be used by other features; check before
  pruning.)
- The orchestrator's special-case handling for trio children in
  `on_task_classified` if any exists.

**Sub-decisions baked in:**

- **Reviewer tools = readonly + UI capture.** Specifically:
  `file_read`, `grep`, `glob`, `git` (diff/log/show only),
  `browse_url`. Rationale: a reviewer that can edit files isn't a
  reviewer, it's a second coder — and that muddies authorship +
  blame. A reviewer that can `bash` can mask bugs by running
  fixes. A reviewer that can `browse_url` can do what a human
  reviewer of a UI PR would: pull up the dev preview and look at
  the rendered page.
- **Coder is allowed to disagree.** The reviewer's feedback is
  advisory until the loop terminates: the coder responds with
  either a fix or a counter-argument. The two converge when both
  agree; if they don't, the architect breaks the tie. This avoids
  the failure mode where a reviewer "knows better than the spec"
  and pushes the coder to over-engineer beyond what the backlog
  item asked for.
- **Round-trip cap = 3.** Empirically generous — most disagreements
  resolve in 1. Cap exists to prevent oscillation when both agents
  are confidently wrong in different directions, in which case the
  architect's wider context is the right tiebreaker.
- **Retry budget on architect tiebreaker = 3 per backlog item.**
  An item can go through the coder/reviewer loop at most 3 times
  before the architect must either mark it done, revise the
  backlog, or escalate via clarification. Stored on
  `architect_attempts` cycle counter, scoped per item.

**What stays the same:**

- Architect logic in `agent/lifecycle/trio/architect.py` — backlog
  JSON shape, clarification flow, checkpoint, revision, the
  retry-on-missing-JSON path added in commit 59d8249.
- `tasks.trio_backlog`, `tasks.trio_phase` columns and their values.
- The architect's commit-and-push of the integration branch.
- Final PR opening at end-of-cycle.
- `architect_attempts` row per architect invocation.

## Consequences

**Easier:**

- One Task row per user request. Concurrency cap means what the user
  thinks it means.
- No deadlock: a trio parent uses exactly one slot for the duration
  of its work, including all backlog items.
- Failure modes collapse: no `task.created` for an unfinished
  internal phase, no `InvalidTransition` in the orchestrator, no
  half-classified child rows lingering after a crash.
- The parent task's tool-call stream becomes the single trace for
  the entire trio cycle — easier to debug, easier to show in the UI
  (already a feed; we just append subagent calls to it).
- Recovery is simpler: `agent/lifecycle/trio/recovery.py` resumes
  `TRIO_EXECUTING` parents; today it ALSO has to worry about
  half-finished children. After this ADR, there are no children to
  worry about.

**Harder:**

- Loses the (theoretical) ability to parallelise backlog items
  across worker processes. In practice the current scheduler is
  serial anyway (`dispatch_next` + `await_child` run sequentially),
  so this is not a real regression — but if we later want parallel
  subagents within a parent we'd add it as `asyncio.gather` over
  multiple `subagent(...)` calls, which is what the skill suggests.
- Subagent has a 10-minute hard timeout per invocation. Today a
  child task can run for much longer. For most backlog items 10m is
  plenty for the coder; the reviewer is faster (readonly + diff +
  one or two `browse_url` calls). The few coder runs that aren't
  will need to be split smaller by the architect. This is a design
  pressure, not a bug — if the architect produces a backlog item
  that needs >10m of coder work, that item was probably the wrong
  shape.
- Worst-case wall clock per backlog item: 3 round-trips × (coder
  ~10m + reviewer ~3m) ≈ 40m, plus possible architect tiebreaker
  re-dispatch. A 4-item backlog could in principle take ~3h on a
  bad day. Today's child-task design takes the same wall clock
  (modulo CI), but the slot was already booked, so this is not a
  regression — just made visible.
- Trio bills against a single slot's heartbeat / watchdog / quota,
  so a long trio (4 items × 8m = 32m) needs to keep the parent's
  heartbeat alive throughout. The subagent tool already heartbeats
  the parent's `task_channel` via `ToolContext.usage_sink`; verify
  this still holds before merging.
- UI loses the "child task tile" list under a parent. Replacement
  is "subagent transcript inline in the parent's tool-call feed" —
  this is arguably better (one pane, scrollable) but is a UI
  redesign rather than a free win.

**Deletion test:** if a future contributor removed this seam and
restored the old "create a Task row per backlog item" design, the
deadlock from problem (1) above would return verbatim. The cost
isn't theoretical; it bit task 170 in production.

**Migration plan (proposed, not part of this ADR's decision):**

- Branch `auto-agent/trio-subagents-NN`.
- Rewrite `agent/lifecycle/trio/scheduler.py` against the subagent
  tool. Keep the same public shape (`dispatch_next` /
  `await_child`) so `run_trio_parent` changes are minimal, OR
  collapse them into a single `dispatch_and_await_subagent`.
- Update `recovery.py` to drop child-task scanning.
- Update `tests/test_trio_*` to mock the subagent tool's
  `execute(...)` instead of seeding child Task rows.
- Keep the `parent_task_id` column for now (other features may
  rely on it); revisit deletion once trio is the only caller.
- No DB migration. No event-taxonomy change.

## Related

- ADR-003 — engineering skills vendoring (introduces the subagent
  tool's prompt and the `superpowers:subagent-driven-development`
  skill).
- ADR-009 — lifecycle phase modules (the architecture this ADR
  extends).
- `agent/tools/subagent.py` — the seam this ADR proposes to use.
- Memory: `architecture_decisions.md` (concurrency cap = 2).
- Incident: task 170 (2026-05-14) — the deadlock that prompted this
  ADR.
