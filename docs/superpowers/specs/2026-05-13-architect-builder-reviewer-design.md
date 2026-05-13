# Architect / Builder / Reviewer Trio — Design Spec

**Date:** 2026-05-13
**Status:** Approved (pending user review of this written spec)
**Scope:** Sub-project B of the 3-spec overhaul. Sub-project A (`bigger-po-market-research`) and Sub-project C (`freeform-self-verification`) have shipped.
**Predecessor brief:** `docs/superpowers/specs/2026-05-13-architect-builder-reviewer-brief.md`.

## Problem

Today, a complex_large task — whether building a cold-start repo or making a substantial change in freeform mode — flows through `planning → coding → verify → review` as a single linear pass. One planner agent decides everything up front, one coding agent implements all of it, one reviewer judges the diff. The model holds the entire scope in one context window for an entire run.

Three failure modes show up at scale:

- **Context exhaustion.** "Build a recipe app with auth, search, and pagination" is too much for one coding pass — the agent forgets earlier decisions, makes inconsistent choices late in the run, or simply runs out of tokens before completing.
- **Lost design continuity across handoffs.** When the planner finishes and the coder takes over, the coder's context is rebuilt from prompts and files. Subtle rationale ("we chose Postgres because the user implied multi-user") is lost the moment the planner exits. The next phase rediscovers, or worse, recontradicts.
- **Reviewer with no design referent.** Today's reviewer judges code quality against general principles. It has no `ARCHITECTURE.md` to compare a builder's diff against, so it can't catch the failure mode "this is well-written code that doesn't match what was supposed to be built."

The fix introduces three roles, each with a dedicated context and a defined protocol:

1. **Architect** — designs the app, holds the long-running design context, makes tradeoff decisions, dispatches work to builders, and ensures nothing slips between hand-offs.
2. **Builder** — implements one bounded work item at a time, against the architect's brief, with a tool to consult the architect when ambiguity surfaces mid-build.
3. **Reviewer** — judges each built work item against the architect's intent and the work item's stated description. Trio reviewer focuses on *alignment* (does the code match what was intended), reusing C's `verify.py` for runtime checks.

Each role runs in its own LLM session, with its own context, communicating through committed artifacts (`ARCHITECTURE.md`, ADRs in `docs/decisions/`, PR descriptions) and explicit tool calls (`consult_architect`, `record_decision`).

## Out of scope (deferred)

- **Replacing today's flow for non-complex_large tasks.** Simple and complex tasks continue using `planning → coding → verify → review`. Trio is reachable only via classifier output of `complex_large`. Migration of other complexity tiers is deferred until the trio proves out in production.
- **Promptfoo eval suite for the trio.** No automated eval cases in v1; judging by hand initially per user direction. Eval follow-up is a known future task.
- **Per-task token meter and cost ceilings.** v1 ships with cycle counts visible in the UI and a manual "Pause Trio" button. Hard cost caps are deferred until observation shows runaway behavior in real usage.
- **Multi-builder parallelism.** Each trio parent dispatches one child at a time, sequentially. Parallel builders are a future optimization.
- **Architect-level review.** Nobody reviews the architect's initial output directly; downstream phases catch design failures via builder consults or reviewer pushback. Adding an architect-review stage is a future option.
- **PR creation as a separate state.** `orchestrator/create_repo.py` continues to create cold-start repos as a pre-task operation (gh API + DB rows); it's not modeled as a state.
- **Cross-task / cross-repo architecture memory via `team-memory`.** Long-term, the architect should call `remember_memory` on durable design decisions so future tasks recall them. v1 ships doc + DB only; `team-memory` integration deferred until that service is healthier.
- **Sub-task PRs sharing a single dev server across cycles.** Each child task boots its own dev server during VERIFYING, as today.

## Architecture

### Routing

Today's `QUEUED → next-state` handler routes by task type. With this spec, it additionally checks `task.complexity`:

```
QUEUED ─┬─► complexity = complex_large ───► TRIO_EXECUTING (new path)
        └─► complexity = simple | complex ─► PLANNING (today's path, unchanged)
```

`orchestrator/create_repo.py` is updated to set `complexity = COMPLEX_LARGE` directly on the scaffold task it creates, bypassing classification (we already know cold-start is large).

### Hierarchical state machine

The trio is modeled as a **super-state with internal transitions**:

- **Outer state** — `TaskStatus.TRIO_EXECUTING` on the trio parent task. Outer transitions: `QUEUED → TRIO_EXECUTING → (DONE | BLOCKED)`.
- **Inner state** — `Task.trio_phase` (nullable enum) on the trio parent, only set while `status = TRIO_EXECUTING`. Internal transitions are owned and validated by the trio module, not by `orchestrator/state_machine.py`.

```
                  ┌─────────────────────────┐
                  │     TRIO_EXECUTING      │
                  │                         │
   QUEUED ──────► │  architecting (initial) │
                  │           │             │
                  │           ▼             │
                  │   awaiting_builder ◄──┐ │
                  │           │           │ │
                  │           ▼           │ │
                  │  [child task runs]    │ │
                  │           │           │ │
                  │           ▼           │ │
                  │  architect_checkpoint─┤ │
                  │           │           │ │
                  │ ┌─────────┴────┐      │ │
                  │ │              │      │ │
                  │ ▼              ▼      │ │
                  │ done       continue ──┘ │
                  │  │         (next item)  │
                  │  │                      │
                  │  │      revise:         │
                  │  │      architecting ───┘ (revision)
                  │  │
                  └──┼──────────────────────┘
                     ▼
                   DONE
```

`Task.trio_phase` values:

- `architecting` — architect is running (either initial or revision sub-phase, distinguished by `architect_attempts.phase`).
- `awaiting_builder` — a child task has been dispatched and is in flight.
- `architect_checkpoint` — child just completed; architect is updating backlog and deciding next action.

### Two task tiers — parent and child

- **Trio parent task** holds the architect's context, `ARCHITECTURE.md`, the backlog, all `architect_attempts` rows. Lifetime: from architect's first run to final backlog drain. No PR of its own. The architect commits to the parent's branch only via initial scaffold + ADRs (see Components).
- **Trio child task** is created per work item. `parent_task_id` points at the trio parent. `complexity = complex`, `freeform_mode` inherited from parent. Each child = one PR, auto-merged on green. Children skip `INTAKE`/`CLASSIFYING` — the parent already knows their shape.

Child task's outer flow:

```
QUEUED ──► CODING ──► VERIFYING ──► TRIO_REVIEW ──► PR_CREATED ──► AWAITING_CI ──► DONE
              ▲           │              │                                │
              │           │              │                                │ (CI fail)
              │           │              │                                ▼
              │           │              │                              CODING
              │           │              │ (alignment fail)
              │           │              ▼
              │           │           CODING
              │           ▼
              │       (boot/intent fail)
              └────  CODING
```

- `CODING` runs today's coding agent, plus the new `consult_architect` tool when the task is a trio child.
- `VERIFYING` is C's existing `verify.py` unchanged.
- `TRIO_REVIEW` is the new alignment check (reads ARCHITECTURE.md + diff + work item description). Replaces `AWAITING_REVIEW` for trio children — verifier already booted and intent-checked; the trio reviewer is novel-work-only.
- `AWAITING_REVIEW` (existing code-level review via `review.py`) is **skipped for trio children.**

### Module layout

```
agent/lifecycle/trio/
├─ __init__.py          # run_trio_parent(task) — owns trio_phase transitions, dispatches children
├─ architect.py         # architect agent for all phases (initial / consult / checkpoint / revision)
├─ scheduler.py         # dispatch_next(parent) and await_child(parent, child) — no LLM
└─ reviewer.py          # trio reviewer for child TRIO_REVIEW state

agent/lifecycle/coding.py  # existing — gains awareness of "is this a trio child?" to expose consult_architect

agent/tools/
├─ consult_architect.py    # new — only exposed to coding agent when task.parent_task_id is a trio parent
└─ record_decision.py      # new — only exposed to architect agent
```

### Repo creation (unchanged)

Cold-start tasks continue to use `orchestrator/create_repo.py` as a pre-task operation: `gh repo create` + `Repo` / `FreeformConfig` / `Task` rows + publish `task.created`. The created task enters the normal pipeline with `complexity = COMPLEX_LARGE` forced, routing it into `TRIO_EXECUTING`.

## Components

### Architect agent — `agent/lifecycle/trio/architect.py`

Runs in four phases, each producing an `architect_attempts` row:

| Phase | Trigger | Output |
|---|---|---|
| `initial` | Parent enters `architecting` for the first time | `ARCHITECTURE.md` written; `trio_backlog` populated; scaffold commands run for cold-start; any initial ADRs |
| `consult` | Builder calls `consult_architect(question, why)` from a child task | Textual answer to builder; optional ARCHITECTURE.md edit; optional ADR |
| `checkpoint` | Child task reaches `DONE`; parent transitions to `architect_checkpoint` | Updated backlog (mark item done; add discovered items); decision: `continue` / `revise` / `done` |
| `revision` | Checkpoint or escalated consult judged the design wrong | Rewritten ARCHITECTURE.md sections; rewritten backlog; new ADRs for changed decisions |

**Tools available:**
- `web_search`, `fetch_url` — for outside grounding during initial design.
- `request_market_brief` — wraps `agent/market_researcher.py::run_market_research`. Used during `initial` (and rarely `revision`) when the task involves product or UX decisions. Resulting `MarketBrief` row attaches to the parent task. The architect cites the brief in `ARCHITECTURE.md`.
- `file_read`, `file_edit`, `file_write` — scoped at the prompt level to `ARCHITECTURE.md`, files under `docs/decisions/`, and scaffold output files. The architect does not write source code.
- `bash` — for scaffold commands (e.g., `npx create-next-app`, `uv init`).
- `glob`, `grep` — read-only exploration.
- `git` — read-only.
- `record_decision` — see below.

**Explicitly not available:** PR creation, running tests, writing source code outside scaffolded files.

**Output to disk:**
- `ARCHITECTURE.md` at the workspace root — the canonical per-app design document. Accreted over the trio's lifetime via `file_edit` calls; fully regenerated only during `revision`.
- ADRs in `docs/decisions/NNN-<slug>.md` — one per non-obvious tradeoff. Format follows the project's existing `docs/decisions/000-template.md`.

**Initial-phase PR.** The architect's first run produces a small commit containing `ARCHITECTURE.md` + scaffolded project files + any initial ADRs. This commit is opened as a PR titled `init: architecture + scaffold` on the parent's branch and auto-merged on green CI — preserving the project invariant that **everything reaches main via PR**, even the architect's own work.

### Builder — `agent/lifecycle/coding.py` (extended)

No new module. The existing coding agent gains:

- A check at agent setup: if `task.parent_task_id` is not null AND `parent.status == TRIO_EXECUTING`, the task is a trio child.
- For trio children, the system prompt is augmented with the full `ARCHITECTURE.md` content + the work item description (which is the child's `task.description`).
- For trio children, the tool registry includes `consult_architect`.
- For trio children, the prompt steers: "You are implementing one bounded work item. If you hit an ambiguity that touches design (file layout, data model, abstraction choice), call `consult_architect`. If you hit a small, code-local ambiguity, decide it yourself."

Everything else — file tools, bash, git, PR creation, test running — is unchanged.

### Trio reviewer — `agent/lifecycle/trio/reviewer.py`

Runs when a child task is in `TRIO_REVIEW` state (between `VERIFYING` and `PR_CREATED`). One agent run per review attempt.

**Inputs (via system prompt):**
- `ARCHITECTURE.md` content.
- The work item description (which will become the PR body verbatim).
- `git diff` of the child's branch against the parent's main.
- Optional access to the dev server if it's still running from `VERIFYING` (rare; the reviewer is text-and-diff focused).

**Tools available:** `file_read`, `glob`, `grep`, `git` read-only, `browse_url` (for occasional spot-checks).

**Verdict shape:** `{ok: bool, feedback: str}` — written to `trio_review_attempts`.

- `ok = true` → child transitions to `PR_CREATED`. The work item's title and description become the PR title and body.
- `ok = false` → child transitions back to `CODING`. Builder sees the feedback in its next turn. The builder may consult the architect or fix directly, depending on the nature of the feedback.

**No severity field.** Any `ok = false` requires the builder to address the feedback before the PR opens. There is no "nit pass-through" — nits are addressed in the same cycle as substantive issues.

### Scheduler — `agent/lifecycle/trio/scheduler.py`

Pure orchestration. No LLM calls.

- `dispatch_next(parent)`:
  1. Reads `parent.trio_backlog`.
  2. Picks the next work item with `status = pending`.
  3. Creates a child `Task` row with: `parent_task_id = parent.id`, `description = work_item.description`, `complexity = complex`, `freeform_mode = parent.freeform_mode`, `status = QUEUED`.
  4. Marks the work item `in_progress` with `assigned_task_id = child.id`.
  5. Publishes `task.created`.
  6. Returns the child task.
- `await_child(parent, child)`:
  1. Subscribes to child status changes via the existing Redis event bus.
  2. Resolves when child reaches `DONE`, `FAILED`, or `BLOCKED`.

Idempotent: if `work_item.assigned_task_id` is already set and the child row exists, `dispatch_next` reuses it instead of creating a new one. This makes scheduler safe to call on crash recovery.

### `consult_architect` tool — `agent/tools/consult_architect.py`

**Signature:** `consult_architect(question: str, why: str) -> str`

The `why` parameter is required and forces the builder to articulate its reason ("blocked on auth pattern choice", "reviewer flagged this as a data model issue"). The why is stored in `architect_attempts.consult_why` for the audit trail.

**Implementation:**
1. Sets `child_task.consulting_architect = True`.
2. Invokes `architect.consult(parent_task, question, why)`, which:
   - Spins up an architect agent run with full context (`ARCHITECTURE.md`, all prior `architect_attempts` for this parent, current backlog).
   - Returns the architect's textual answer.
   - Persists an `architect_attempts` row with `phase = 'consult'` and the question + why fields populated.
   - If the consult mutated `ARCHITECTURE.md`, the commit SHA is recorded on the row.
3. Clears `child_task.consulting_architect` in a `finally` block.
4. Returns the answer to the builder agent. If the architect updated `ARCHITECTURE.md`, the answer is prefixed with: `Note: ARCHITECTURE.md was updated; re-read before continuing.`

**Failure modes:**
- LLM provider error → existing retry/backoff handles; on exhaustion, returns an error string to the builder. Builder can retry or pivot.
- Architect returns "I don't know, need human input" — only reachable for non-freeform tasks. Returns that text verbatim. For freeform tasks, the architect is forbidden from this output (see Error handling).

### `record_decision` tool — `agent/tools/record_decision.py`

**Signature:** `record_decision(title: str, context: str, decision: str, consequences: str) -> str`

Only available to the architect agent. Writes a new ADR to `docs/decisions/NNN-<slug>.md` where:

- `NNN` is the next sequential 3-digit number after the highest existing ADR in the directory.
- `<slug>` is derived from `title` (lowercase, alphanumeric + hyphens, ≤ 40 chars).
- The body uses the format established by `docs/decisions/000-template.md`.

Returns the file path. The architect commits the ADR as part of the same git commit as any related `ARCHITECTURE.md` change.

## Data Model

### Enum additions

```python
# shared/models.py — TaskStatus
TRIO_EXECUTING = "trio_executing"   # outer state, trio parent only
TRIO_REVIEW    = "trio_review"      # child task state, between VERIFYING and PR_CREATED

# new enum
class TrioPhase(str, enum.Enum):
    ARCHITECTING         = "architecting"
    AWAITING_BUILDER     = "awaiting_builder"
    ARCHITECT_CHECKPOINT = "architect_checkpoint"
```

### New columns on `Task`

| Column | Type | Purpose |
|---|---|---|
| `parent_task_id` | `Integer, FK(tasks.id), nullable` | Generic child→parent link. Trio is v1 user; reusable for any future parent/child pattern. |
| `trio_phase` | `Enum(TrioPhase), nullable` | Inner state when `status = TRIO_EXECUTING`. Null otherwise. |
| `trio_backlog` | `JSONB, nullable` | Architect's working backlog. Null until first architect run. |
| `consulting_architect` | `Boolean, default False` | Transient flag on a trio child while `consult_architect` is in flight. UI renders the spinner state from this column. |

### Backlog shape

```python
class WorkItem(BaseModel):
    id: str                              # uuid4 — stable across architect checkpoints
    title: str                           # becomes PR title
    description: str                     # becomes PR body / builder prompt
    status: Literal['pending', 'in_progress', 'done', 'skipped']
    priority: Literal['core', 'nit'] = 'core'   # see note below
    assigned_task_id: int | None = None
    discovered_in_attempt_id: int | None = None
```

Stored as `list[WorkItem]` under `Task.trio_backlog`. Architect mutations are full backlog rewrites per checkpoint — JSONB is fine for this scale (work items per task are expected to be low double digits).

**Note on `priority`.** The `priority` field is retained for future use (architect may auto-bundle multiple nits into a single PR rather than dispatching N tiny PRs). v1 always treats every backlog item identically — `priority` is informational.

### New audit tables

```python
class ArchitectAttempt(Base):
    __tablename__ = "architect_attempts"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, FK(tasks.id), nullable=False, index=True)  # always the trio PARENT
    phase = Column(
        Enum("initial", "consult", "checkpoint", "revision", name="architect_phase"),
        nullable=False,
    )
    cycle = Column(Integer, nullable=False)
    reasoning = Column(Text, nullable=False)
    decision = Column(JSONB, nullable=True)
    consult_question = Column(Text, nullable=True)
    consult_why = Column(Text, nullable=True)
    architecture_md_after = Column(Text, nullable=True)
    commit_sha = Column(String(40), nullable=True)
    tool_calls = Column(JSONB, nullable=False, default=list)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

class TrioReviewAttempt(Base):
    __tablename__ = "trio_review_attempts"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, FK(tasks.id), nullable=False, index=True)  # always the trio CHILD
    cycle = Column(Integer, nullable=False)
    ok = Column(Boolean, nullable=False)
    feedback = Column(Text, nullable=False)   # empty string when ok=true
    tool_calls = Column(JSONB, nullable=False, default=list)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
```

No separate `builder_attempts` table — each trio child task is itself the record of a builder cycle. The child task's existing fields + the agent's tool-call audit trail (via existing infrastructure) carry the per-cycle audit information.

### State machine transitions

```python
# orchestrator/state_machine.py STATE_TRANSITIONS additions
TaskStatus.QUEUED:         {..., TaskStatus.TRIO_EXECUTING}     # router branches on complexity
TaskStatus.TRIO_EXECUTING: {TaskStatus.DONE, TaskStatus.BLOCKED}  # parent: drained or stuck
TaskStatus.VERIFYING:      {..., TaskStatus.TRIO_REVIEW}        # added for trio children
TaskStatus.TRIO_REVIEW:    {TaskStatus.PR_CREATED, TaskStatus.CODING, TaskStatus.BLOCKED}
TaskStatus.CODING:         {..., TaskStatus.TRIO_REVIEW}        # added for trio children
```

`TaskStatus.AWAITING_REVIEW` transitions are unchanged — that state is skipped entirely for trio children.

### Migration

One Alembic migration: `migrations/versions/033_trio.py`. Idempotent enum adds (pattern from migration 032), new columns on `tasks`, two new tables.

```sql
ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'trio_executing';
ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'trio_review';
CREATE TYPE triophase AS ENUM ('architecting', 'awaiting_builder', 'architect_checkpoint');
CREATE TYPE architect_phase AS ENUM ('initial', 'consult', 'checkpoint', 'revision');

ALTER TABLE tasks ADD COLUMN parent_task_id INTEGER REFERENCES tasks(id);
ALTER TABLE tasks ADD COLUMN trio_phase triophase;
ALTER TABLE tasks ADD COLUMN trio_backlog JSONB;
ALTER TABLE tasks ADD COLUMN consulting_architect BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE architect_attempts (...);
CREATE TABLE trio_review_attempts (...);
```

### `shared/models.py` split

`shared/models.py` is already 624 lines (past the project's 500-line guideline; loose-end #11 from the brief). The trio adds two more ORM classes plus two new enums. As part of this migration:

```
shared/models/
├─ __init__.py            # re-exports for backwards-compat with `from shared.models import X`
├─ core.py                # Task, Repo, Plan, Organization, OrganizationMembership, User
├─ freeform.py            # FreeformConfig, Suggestion, VerifyAttempt, ReviewAttempt, MarketBrief
└─ trio.py                # ArchitectAttempt, TrioReviewAttempt
```

`__init__.py` re-exports every previously-public class so no callers need to change imports.

## Error Handling

### BLOCKED semantics

The trio has four BLOCKED paths, all routed through the existing `BLOCKED` outer state on the parent task:

| Path | Trigger | Applies to |
|---|---|---|
| Architect declares clarification needed | Architect emits `decision={action: 'awaiting_clarification', question}` | **Non-freeform tasks only** — transitions parent to existing `AWAITING_CLARIFICATION` |
| Child task BLOCKED | A trio child enters `BLOCKED` (quota, repeated CI failure, etc.) | All trio tasks |
| Child task FAILED | Unrecoverable error in child | All trio tasks |
| Manual cancellation | User triggers "Pause Trio" in web-next | All trio tasks |

`parent.trio_phase` is cleared on transition to BLOCKED. The latest `architect_attempts` row (or child's BLOCKED reason) carries the audit explanation. UI renders the cause from those rows.

**In-flight children when parent enters BLOCKED:** allowed to finish their current state (no LLM-call interruption). New children are not dispatched. An orphan child reaching DONE after parent BLOCKED is a successful merged PR — fine, work merged is work merged.

### Architect autonomy in freeform mode

**When `parent.freeform_mode == True`, the architect is forbidden from declaring it cannot proceed.** It must make decisions on tradeoffs and document them as ADRs in `docs/decisions/`.

The architect's system prompt steers explicitly:

> This task is in freeform mode (`task.freeform_mode = True`). You cannot ask for human input. When facing a non-obvious tradeoff (stack choice, data model, ambiguous requirement), make a reasoned call, call `record_decision` to log the rationale, and continue. The human reviews ADRs after the work ships.

For non-freeform tasks (Slack / Linear / Telegram intake with `complexity = complex_large`), the architect may still emit `decision={action: 'awaiting_clarification'}`, which transitions the parent to today's `AWAITING_CLARIFICATION` state.

ADRs are surfaced in the parent task's web-next page as a "Decisions" panel listing each ADR committed during the trio's lifetime — the human's feedback loop.

### Loopback policy

**No hard or soft caps anywhere.** Per user direction, the agents are allowed to loop as much as they require.

Safety comes from observability:
- All audit tables are append-only; every cycle produces a row.
- web-next renders cycle counts on the parent task page ("Backlog: 4 of 11 done — 18 builder cycles, 12 reviews so far").
- A "Pause Trio" button transitions the parent to `BLOCKED` manually. This is the kill switch.

**Followup note (not v1):** if runaway loops show up in real usage, the natural follow-up is per-task token metering (a visible cost ceiling per parent), not hardcoded cycle limits.

### Crash recovery

On orchestrator startup, scan for stuck trio parents and resume:

```python
stuck_trio_parents = await find_tasks_in_status(TRIO_EXECUTING)
for parent in stuck_trio_parents:
    asyncio.create_task(resume_trio_parent(parent))

async def resume_trio_parent(parent):
    match parent.trio_phase:
        case ARCHITECTING:
            # If latest architect_attempts row has commit_sha, work was persisted: advance phase.
            # If row exists but commit_sha is null, architect crashed before commit: re-run.
            # If no row at all, run fresh.
        case AWAITING_BUILDER:
            # Find child via the latest work_item.assigned_task_id.
            # If child exists and not terminal, await it.
            # If child doesn't exist, dispatch.
        case ARCHITECT_CHECKPOINT:
            # If latest checkpoint attempt has a decision, act on it.
            # Else, re-run checkpoint.
```

Three idempotency invariants the trio relies on (enforced inside the trio module):

1. **Commits are the atomic unit.** Each architect phase that mutates the workspace results in a git commit, with the SHA stored on the `architect_attempts` row. Recovery checks for committed state before re-running.
2. **Scheduler checks `work_item.assigned_task_id`** before dispatching to avoid duplicate children.
3. **Checkpoint reads from merged commits**, not from in-memory caches. Re-running sees the same code.

### `consult_architect` failure modes

- **Provider error.** Existing retry/backoff in `BedrockProvider.complete` handles transient errors. On exhaustion, tool returns an error string; builder can retry or skip.
- **Architect declines (non-freeform only).** Tool returns the architect's text verbatim. Builder either pivots or also gives up (child → BLOCKED).
- **Tool itself crashes.** `consulting_architect` is cleared in a `finally` block; exception surfaces to the agent loop normally.

### Workspace lifecycle

- **Trio parent** has its own workspace (where the architect commits ARCHITECTURE.md and runs scaffold commands).
- **Each child task** gets its own workspace on its own branch via the existing `agent/workspace.py` pattern.
- On parent → DONE: parent workspace cleaned per existing cleanup logic.
- On parent → BLOCKED: parent workspace **retained** for human inspection (matches existing BLOCKED behavior).

### Bundled loose-end fixes

The trio touches code that already had known issues. Fixed as part of this work:

- **Loose-end #1: dev-server log leak in `/tmp`.** `kill_server` in `agent/tools/dev_server.py` now `os.unlink(handle.log_path)` to clean up. Trio runs verify per-child, so the leak gets worse without this fix.
- **Loose-end #11: `shared/models.py` split.** Done as part of the migration (see Data Model).
- **Loose-ends #2, #3, #4:** verify.py hardening — `BootError` catch, `branch_name` None guard, `wait_for` envelope scope correction. Verify runs much more often under the trio; hardening first.

Loose-ends explicitly deferred: #5 (TOCTOU port race), #6 (OK-regex edge cases), #7 (test_verify_review_models DB requirement), #8 (verify/review eval), #9 (AttemptsPanel screenshot thumbnails), #10 (screenshot disk persistence), #12 (capability-bundle builder pattern).

## Testing

### The load-bearing regression test

`tests/test_trio_rejects_obvious_flaw_despite_agent_ok.py` — TDD keystone. End-to-end fixture:

1. A trio parent task is created with a known-good description ("Build a TODO app").
2. Architect runs against a stub LLM that returns a fixed ARCHITECTURE.md + 2-item backlog.
3. Builder runs against a stub LLM that produces code with a deliberate, observable flaw (e.g., the main page renders only `Lorem ipsum`).
4. Trio reviewer runs against the real prompt + real LLM call (or a recorded transcript replaying actual review judgment).
5. **Assert: reviewer returns `ok = false` despite all upstream agents having claimed success.**

This is the moat against the failure mode "everybody agrees, but the result is bad." Write it red, watch it stay red until the reviewer prompt is good enough.

### Layered test pyramid

**Unit tests** (fast, deterministic, stubbed LLMs):

- `tests/test_trio_state_machine.py` — every legal/illegal transition on `Task.trio_phase` and `TaskStatus` for trio paths.
- `tests/test_trio_scheduler.py` — `dispatch_next` picks the right work item; doesn't dispatch when `assigned_task_id` is set; `await_child` resolves on terminal child states.
- `tests/test_architect_attempts.py` — each phase writes the expected row shape with the right `phase` enum; commit_sha recorded when committed.
- `tests/test_trio_review_attempts.py` — verdict shape; transitions on ok / !ok.
- `tests/test_record_decision_tool.py` — writes ADR to `docs/decisions/`; increments numbering correctly; commits.
- `tests/test_consult_architect_tool.py` — sets/clears `consulting_architect`; appends `architect_attempts` row; returns answer string. Failure paths (provider error, architect declines) return correct error strings.
- `tests/test_trio_recovery.py` — various crash scenarios (architect row with no commit_sha, child mid-flight, etc.) → recovery picks the right next action.

**Integration tests** (slower, real DB, stubbed LLMs):

- `tests/test_trio_e2e_happy_path.py` — full trio: classify → trio_executing → architect → 2 children → checkpoint → done. Deterministic LLM responses drive each phase.
- `tests/test_trio_e2e_block_path.py` — child task BLOCKED triggers parent BLOCKED; in-flight assertions on `trio_phase`.
- `tests/test_trio_models_migration.py` — requires real Postgres at HEAD 033; verifies tables/columns/enums materialized. Same skip-on-no-db pattern as `test_verify_review_models.py`.

**E2E test** (slow, against real Playwright + real http.server fixture):

- `tests/test_trio_review_smoke.py` — extends the existing Playwright smoke. Stub LLMs return a small TODO-app build; trio reviewer runs against the dev server's screenshot + ARCHITECTURE.md; assert verdict on a real visual.

### Coverage gaps and explicit non-goals

- **No multi-agent prompt eval in v1.** Architect / builder / reviewer prompts iterate via real LLM runs and hand judgment per user direction.
- **No load test for many concurrent trio tasks.** Existing `MAX_CONCURRENT_TASKS=2` config applies; no new concurrency model.
- **No fuzz on `record_decision` slug generation.** Tested for the obvious case (no special chars); broader fuzzing deferred.

### TDD ordering

The plan will land tasks in approximately this order, each red→green:

1. Migration 033 + `shared/models.py` split → models import test.
2. Pydantic types in `shared/types.py` for `WorkItem`, `TrioPhase`, decision shapes → JSON round-trip test.
3. `consult_architect` tool unit tests → tool impl.
4. `record_decision` tool unit tests → tool impl.
5. Architect agent with `phase = initial` under stub LLM → architect_attempts row test.
6. Scheduler unit tests → scheduler impl.
7. Trio orchestrator state machine tests → trio orchestrator impl.
8. Child task with `TRIO_REVIEW` state transitions → state machine test.
9. Trio reviewer agent under stub LLM → trio_review_attempts test.
10. **Load-bearing test:** `test_trio_rejects_obvious_flaw_despite_agent_ok.py`.
11. Recovery tests for each phase.
12. web-next: parent task page + child task page + ADR panel + Pause Trio button.
13. Bundled loose-end fixes (dev-server log leak; verify.py hardening).

## Acceptance Criteria

The trio is complete when:

- A task with `complexity = COMPLEX_LARGE` routes to `TRIO_EXECUTING` instead of `PLANNING`. Other complexities unchanged.
- A cold-start task created via `/freeform "build something new"` is automatically classified as `COMPLEX_LARGE` and enters the trio.
- The architect's `initial` phase produces `ARCHITECTURE.md`, a populated `trio_backlog`, and (for cold-start) a scaffolded project, committed and merged via the `init: architecture + scaffold` PR.
- The trio dispatches child tasks sequentially, one per backlog item. Each child opens a PR titled with the work item title, body equal to the work item description.
- Each child PR is gated by `VERIFYING` (boot + intent) and `TRIO_REVIEW` (alignment) before opening. `AWAITING_REVIEW` is skipped.
- The builder can call `consult_architect(question, why)` from any trio child. The call appends an `architect_attempts` row with `phase = 'consult'` and (if applicable) commits an ARCHITECTURE.md update.
- The architect can call `record_decision(title, context, decision, consequences)`. The tool writes a properly-formatted ADR to `docs/decisions/NNN-<slug>.md` and commits it.
- For freeform-mode trio tasks, the architect never returns "awaiting clarification" — it makes decisions, logs ADRs, and continues.
- web-next renders: trio_phase on the parent task page, cycle counts visible, ADR panel listing decisions, child task drill-down with consulting_architect spinner state, a "Pause Trio" button.
- On orchestrator restart, in-flight trio tasks resume cleanly to the correct phase per the recovery logic.
- The load-bearing regression test (`test_trio_rejects_obvious_flaw_despite_agent_ok.py`) is green.
- Migration 033 applies cleanly; rollback path tested.
- All bundled loose-end fixes (dev-server log leak; verify.py hardening) pass their tests.

## Future Work

- **`team-memory` integration.** Architect calls `remember_memory` on durable cross-task decisions during initial / revision phases. Deferred until team-memory is healthier infrastructure.
- **Promptfoo eval suite for the trio.** Bootstrap-a-recipe-app and similar cases as scored eval. Deferred to first follow-up.
- **Per-task token meter and cost ceiling.** Triggered if real-world runaway loops show up.
- **Migrate complex (not just complex_large) tasks to the trio.** Decision deferred until the trio proves out for complex_large.
- **Parallel builders.** Multiple children dispatched concurrently. Decision deferred until sequential proves too slow.
- **Sub-task PR sharing of a single dev server.** Reduce verify boot overhead per child. Decision deferred until boot cost matters.
- **Architect-level review.** Adding an explicit review stage on the architect's initial output. Decision deferred until experience shows it's needed.
