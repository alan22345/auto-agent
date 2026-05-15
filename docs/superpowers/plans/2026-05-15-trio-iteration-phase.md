# Trio Iteration Phase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire up ADR-017 — let a user post feedback on a trio task's open PR via web UI, Slack, Telegram, or GitHub PR comment and have the trio dispatcher re-iterate (architect → backlog → builder → reviewer → smoke → additive commits). PR_CREATED becomes a one-shot transit event; AWAITING_REVIEW + new ITERATING are the long-lived states.

**Architecture:** New `iteration.py` module entry point + new `architect.iterate(...)` entry, both wired through the existing `run_trio_parent` dispatcher via a new `iteration_context` kwarg parallel to `repair_context`. All four channels normalise onto `human.message`; `route_human_message` routes trio tasks in {AWAITING_REVIEW, ITERATING} to `iteration.handle_iteration_feedback`. No force-push — the per-item builder already pushes additively.

**Tech Stack:** Python 3.12 + asyncio, FastAPI, SQLAlchemy async, Alembic, Postgres, Redis (events + per-task channel), pytest, ruff.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `migrations/versions/045_iteration_phase.py` | create | Add `ITERATING` to `taskstatus`, `ARCHITECT_ITERATING` to `triophase` |
| `shared/models/core.py` | modify | Add `ITERATING` enum member to `TaskStatus`, `ARCHITECT_ITERATING` to `TrioPhase` |
| `shared/events.py` | modify | Add `TaskEventType.ITERATION_COMPLETE` + `task_iteration_complete(...)` factory |
| `agent/lifecycle/trio/__init__.py` | modify | Auto-fall-through PR_CREATED → AWAITING_REVIEW; accept `iteration_context` in `run_trio_parent`; iteration-tail transition + event |
| `agent/lifecycle/trio/architect.py` | modify | New `iterate(parent_id, iteration_context)` entry point |
| `agent/lifecycle/trio/iteration.py` | create | `handle_iteration_feedback(task_id, message)` entry + `_active_iteration_tasks` guard |
| `agent/lifecycle/conversation.py` | modify | `route_human_message` branch for AWAITING_REVIEW/ITERATING + trio; broaden `handle_feedback_event` to re-emit `human.message` for those statuses |
| `orchestrator/router.py` | modify | `POST /tasks/{id}/message` (singular) also publishes `human.message` |
| `integrations/slack/main.py` | modify | Add `_fmt_task_iteration_complete` formatter |
| `integrations/telegram/main.py` | modify | Add iteration-complete dispatcher entry |
| `tests/test_iteration_phase_*.py` | create | Unit + integration tests per task |

---

### Task 1: Migration — add ITERATING and ARCHITECT_ITERATING enum values

**Files:**
- Create: `migrations/versions/045_iteration_phase.py`

- [ ] **Step 1: Write the migration**

```python
"""ADR-017 — add ITERATING taskstatus + ARCHITECT_ITERATING triophase.

Both are idempotent ``ADD VALUE IF NOT EXISTS`` so the migration is safe
to re-run on stacks that already applied it manually.

Revision ID: 045
Revises: 044
Create Date: 2026-05-15
"""

from __future__ import annotations

from alembic import op

revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'ITERATING'")
    op.execute("ALTER TYPE triophase ADD VALUE IF NOT EXISTS 'ARCHITECT_ITERATING'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE DROP VALUE. Downgrade is a no-op — enum values
    # left in place don't affect anything that hasn't been written to use them.
    pass
```

- [ ] **Step 2: Verify the file lints and parses**

Run: `.venv/bin/python3 -m ruff check migrations/versions/045_iteration_phase.py`
Expected: `All checks passed!`

- [ ] **Step 3: Commit**

```bash
git add migrations/versions/045_iteration_phase.py
git commit -m "feat(adr-017): migration — add ITERATING + ARCHITECT_ITERATING enums"
```

---

### Task 2: Add enum members to SQLAlchemy models

**Files:**
- Modify: `shared/models/core.py` — `TaskStatus` and `TrioPhase` enums
- Test: `tests/test_iteration_phase_enums.py`

- [ ] **Step 1: Write the failing test**

```python
"""ADR-017 — TaskStatus.ITERATING + TrioPhase.ARCHITECT_ITERATING."""

from __future__ import annotations

from shared.models import TaskStatus, TrioPhase


def test_task_status_iterating_exists():
    assert TaskStatus.ITERATING.value == "iterating"


def test_trio_phase_architect_iterating_exists():
    assert TrioPhase.ARCHITECT_ITERATING.value == "architect_iterating"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_iteration_phase_enums.py -q`
Expected: FAIL — `AttributeError: ITERATING` (or `ARCHITECT_ITERATING`).

- [ ] **Step 3: Find current TaskStatus and TrioPhase enums and add members**

In `shared/models/core.py`, locate `class TaskStatus(str, enum.Enum):` and add a new member after the last entry:

```python
    ITERATING = "iterating"  # ADR-017: trio is re-iterating a PR on user feedback
```

Then locate `class TrioPhase(str, enum.Enum):` and add:

```python
    ARCHITECT_ITERATING = "architect_iterating"  # ADR-017
```

(Hint: `grep -n "class TaskStatus\|class TrioPhase" shared/models/core.py` to find them.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_iteration_phase_enums.py -q`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add shared/models/core.py tests/test_iteration_phase_enums.py
git commit -m "feat(adr-017): add ITERATING/ARCHITECT_ITERATING enum members"
```

---

### Task 3: Event factory — `task_iteration_complete`

**Files:**
- Modify: `shared/events.py` — add enum member + factory
- Test: `tests/test_events_taxonomy.py` (extend)

- [ ] **Step 1: Add the failing test (extend existing test_events_taxonomy.py)**

Append to `tests/test_events_taxonomy.py`:

```python
def test_task_iteration_complete():
    from shared.events import TaskEventType, task_iteration_complete

    ev = task_iteration_complete(task_id=42, summary="updated PR with your changes")
    assert ev.type == TaskEventType.ITERATION_COMPLETE
    assert ev.task_id == 42
    assert ev.payload == {"summary": "updated PR with your changes"}
```

Also add `task_iteration_complete` to the imports at the top of the file (look for the existing `from shared.events import (...)` block).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_events_taxonomy.py::test_task_iteration_complete -q`
Expected: FAIL — import error or AttributeError on `ITERATION_COMPLETE`.

- [ ] **Step 3: Add the enum member and factory**

In `shared/events.py`, locate `class TaskEventType(StrEnum):` and add (alphabetical insertion is fine, just stay inside the class):

```python
    ITERATION_COMPLETE = "task.iteration_complete"
```

Then near the other factories (e.g. just after `task_pr_created`), add:

```python
def task_iteration_complete(task_id: int, *, summary: str = "") -> Event:
    """ADR-017 — one trio iteration cycle finished pushing additive commits
    to the integration branch. Slack/Telegram notifier renders this as
    'updated PR with your changes'."""
    return Event(
        type=TaskEventType.ITERATION_COMPLETE,
        task_id=task_id,
        payload={"summary": summary},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_events_taxonomy.py::test_task_iteration_complete -q`
Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add shared/events.py tests/test_events_taxonomy.py
git commit -m "feat(adr-017): task_iteration_complete event factory"
```

---

### Task 4: Web UI `/message` (singular) publishes `human.message`

**Files:**
- Modify: `orchestrator/router.py` — the `add_task_message` handler (`@router.post("/tasks/{task_id}/message")`, ~line 1539)
- Test: `tests/test_iteration_phase_message_endpoint.py`

- [ ] **Step 1: Write the failing test**

```python
"""ADR-017 — POST /tasks/{id}/message (singular) must publish human.message
so user feedback from the legacy web UI reaches route_human_message."""

from __future__ import annotations

import os
import uuid
from unittest.mock import patch

import pytest

from shared.events import HumanEventType


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_post_singular_message_publishes_human_message(session, publisher):
    from shared.models import (
        Organization,
        Repo,
        Task,
        TaskComplexity,
        TaskSource,
        TaskStatus,
    )
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"adr17-{suffix}", slug=f"adr17-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(
        name=f"r-{suffix}", url="https://github.com/o/r.git",
        organization_id=org.id, default_branch="main",
    )
    session.add(repo)
    await session.flush()
    task = Task(
        title="T", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_REVIEW,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
    )
    session.add(task)
    await session.commit()

    from orchestrator.router import add_task_message
    from shared.types import TaskMessageRequest

    # Bypass auth dependency by calling the function directly with explicit args.
    with patch("orchestrator.router._get_task_in_org", return_value=task):
        await add_task_message(
            task_id=task.id,
            req=TaskMessageRequest(message="break it down further", username="alan"),
            session=session,
            org_id=org.id,
        )

    matches = [
        e for e in publisher.events
        if e.type == HumanEventType.MESSAGE and e.task_id == task.id
    ]
    assert len(matches) == 1, f"expected 1 human.message event, got {len(matches)}"
    assert matches[0].payload["message"] == "break it down further"
    assert matches[0].payload["source"] == "web"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `DATABASE_URL=$DATABASE_URL .venv/bin/python3 -m pytest tests/test_iteration_phase_message_endpoint.py -q`
Expected: FAIL (no human.message event published) — or skip if DATABASE_URL unset (test will run in CI / on VM).

- [ ] **Step 3: Modify the endpoint**

In `orchestrator/router.py`, locate `async def add_task_message(...)` (search for `@router.post("/tasks/{task_id}/message")` — note: singular, no `s`). Currently it inserts a `TaskHistory` row and returns `{"ok": True}`. Add `human_message` publish after the insert:

```python
@router.post("/tasks/{task_id}/message")
async def add_task_message(
    task_id: int,
    req: TaskMessageRequest,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> dict:
    """Persist a human message in the task's history."""
    task = await _get_task_in_org(session, task_id, org_id)
    if not task:
        raise HTTPException(404, "Task not found")
    session.add(
        TaskHistory(
            task_id=task.id,
            from_status=task.status,
            to_status=task.status,  # No transition — just a message log
            message=f"[{req.username}] {req.message}",
        )
    )
    await session.commit()
    # ADR-017 — surface the message as a human.message event so
    # route_human_message can dispatch it (e.g. to trio iteration).
    await publish(human_message(task_id=task.id, message=req.message, source="web"))
    return {"ok": True}
```

If `human_message` and `publish` aren't already imported in `router.py`, add them to the top-level imports from `shared.events`.

- [ ] **Step 4: Run test to verify it passes**

Run: `DATABASE_URL=$DATABASE_URL .venv/bin/python3 -m pytest tests/test_iteration_phase_message_endpoint.py -q`
Expected: `1 passed` (or `1 skipped` if no DATABASE_URL — still acceptable, the VM CI will run it).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/router.py tests/test_iteration_phase_message_endpoint.py
git commit -m "feat(adr-017): POST /tasks/{id}/message also publishes human.message"
```

---

### Task 5: Auto-fall-through PR_CREATED → AWAITING_REVIEW after integration PR opens

**Files:**
- Modify: `agent/lifecycle/trio/__init__.py` — `_open_integration_pr_and_transition` (~line 962)
- Test: `tests/test_iteration_phase_pr_open_transitions.py`

- [ ] **Step 1: Write the failing test**

```python
"""ADR-017 — after opening the integration PR, trio auto-falls-through
PR_CREATED → AWAITING_REVIEW. PR_CREATED becomes a single-fire transit
event; AWAITING_REVIEW is the long-lived "PR open" state."""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_open_integration_pr_falls_through_to_awaiting_review(session):
    from shared.models import (
        Organization,
        Repo,
        Task,
        TaskComplexity,
        TaskSource,
        TaskStatus,
    )
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"r-{suffix}", slug=f"r-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(
        name=f"r-{suffix}", url="https://github.com/o/r.git",
        organization_id=org.id, default_branch="main",
    )
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.FINAL_REVIEW,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
    )
    session.add(parent)
    await session.commit()

    from agent.lifecycle.trio import _open_integration_pr_and_transition

    with patch(
        "agent.lifecycle.trio._open_integration_pr",
        AsyncMock(return_value="https://github.com/o/r/pull/42"),
    ):
        await _open_integration_pr_and_transition(parent=parent, target_branch="main")

    await session.refresh(parent)
    assert parent.status == TaskStatus.AWAITING_REVIEW, (
        f"expected fall-through to AWAITING_REVIEW, got {parent.status}"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `DATABASE_URL=$DATABASE_URL .venv/bin/python3 -m pytest tests/test_iteration_phase_pr_open_transitions.py -q`
Expected: FAIL — status is `PR_CREATED`, not `AWAITING_REVIEW`.

- [ ] **Step 3: Modify `_open_integration_pr_and_transition`**

In `agent/lifecycle/trio/__init__.py`, find the function (~line 962). Look for the existing transition:

```python
        await transition(s, p, TaskStatus.PR_CREATED, message="trio: integration PR opened")
```

Right after that transition (still inside the `async with async_session() as s:` block), add the immediate fall-through:

```python
        await transition(s, p, TaskStatus.PR_CREATED, message="trio: integration PR opened")
        # ADR-017 — PR_CREATED is a single-fire transit event; AWAITING_REVIEW
        # is the long-lived state. Fall through immediately so the task lands
        # in the right phase for the iteration loop to engage.
        await transition(s, p, TaskStatus.AWAITING_REVIEW, message="trio: awaiting review/feedback")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `DATABASE_URL=$DATABASE_URL .venv/bin/python3 -m pytest tests/test_iteration_phase_pr_open_transitions.py -q`
Expected: `1 passed` (or `1 skipped`).

- [ ] **Step 5: Verify state-machine accepts PR_CREATED → AWAITING_REVIEW**

Check `orchestrator/state_machine.py` (or wherever `transition()` lives — `grep -rn "def transition\b" orchestrator shared`) and confirm the PR_CREATED → AWAITING_REVIEW edge is allowed. If it isn't, add it to the allowed-transitions map. If you add it, also update any state-machine tests that enumerate edges.

- [ ] **Step 6: Commit**

```bash
git add agent/lifecycle/trio/__init__.py tests/test_iteration_phase_pr_open_transitions.py
# only stage state_machine.py if you actually changed it
git commit -m "feat(adr-017): trio integration-PR open falls through to AWAITING_REVIEW"
```

---

### Task 6: `architect.iterate` entry point

**Files:**
- Modify: `agent/lifecycle/trio/architect.py` — add new `iterate(...)` function
- Test: `tests/test_iteration_phase_architect_iterate.py`

- [ ] **Step 1: Write the failing test**

```python
"""ADR-017 — architect.iterate produces a fresh backlog appended to the
existing trio_backlog, with the user feedback + PR diff + design.md in
the pinned context."""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_iterate_appends_to_existing_backlog(session):
    from sqlalchemy import select

    from agent.lifecycle.trio import architect
    from shared.models import (
        Organization,
        Repo,
        Task,
        TaskComplexity,
        TaskSource,
        TaskStatus,
    )
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"r-{suffix}", slug=f"r-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(
        name=f"r-{suffix}", url="https://github.com/o/r.git",
        organization_id=org.id, default_branch="main",
    )
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.ITERATING,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        trio_backlog=[
            {"id": "S1", "title": "x", "description": "", "status": "done", "head_sha": "abc"},
        ],
    )
    session.add(parent)
    await session.commit()

    # Stub everything that touches workspace/LLM: we're testing the
    # architect.iterate plumbing, not the agent loop.
    with (
        patch.object(architect, "_prepare_parent_workspace",
                     AsyncMock(return_value="/tmp/ws")),
        patch.object(architect, "create_architect_agent") as mock_factory,
    ):
        # Architect run produces a fresh backlog of 1 new pending item.
        agent_loop = AsyncMock()
        agent_loop.run = AsyncMock(return_value=type("R", (), {
            "output": '{"decision": {"action": "submit_backlog", "backlog": ['
                      '{"id": "S2", "title": "address feedback", '
                      '"description": "...", "status": "pending"}'
                      ']}}',
            "messages": [],
        })())
        mock_factory.return_value = agent_loop

        await architect.iterate(
            parent.id,
            iteration_context={"feedback": "break it down further",
                               "pr_url": "https://x/pull/1"},
        )

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent.id))
    ).scalar_one()
    ids = [item["id"] for item in (refreshed.trio_backlog or [])]
    assert "S1" in ids, "existing done item dropped"
    assert "S2" in ids, "new pending item not appended"
    assert next(i for i in refreshed.trio_backlog if i["id"] == "S2")["status"] == "pending"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `DATABASE_URL=$DATABASE_URL .venv/bin/python3 -m pytest tests/test_iteration_phase_architect_iterate.py -q`
Expected: FAIL — `AttributeError: iterate`.

- [ ] **Step 3: Implement `architect.iterate`**

In `agent/lifecycle/trio/architect.py`, near `run_initial` and `checkpoint`, add:

```python
async def iterate(parent_id: int, iteration_context: dict) -> None:
    """ADR-017 — re-iterate a complex_large task on user feedback.

    Reads .auto-agent/design.md + the integration branch's PR diff +
    existing trio_backlog, plus the user's feedback message, and asks
    the architect to emit a fresh backlog of items needed to address
    the feedback. New items are APPENDED to trio_backlog (not replaced)
    so the audit of what shipped originally survives. Already-done items
    are left alone; the per-item dispatcher only processes ``pending``.
    """
    from sqlalchemy import select

    from shared.database import async_session
    from shared.models import Task

    async with async_session() as s:
        parent = (
            await s.execute(select(Task).where(Task.id == parent_id))
        ).scalar_one()

    workspace = await _prepare_parent_workspace(parent)
    workspace_root = workspace.root if hasattr(workspace, "root") else str(workspace)

    # Build pinned context: design doc, PR diff, existing backlog, feedback.
    design_md = _read_design_md(workspace_root, parent.id)
    pr_diff = await _read_pr_diff(workspace_root, parent)
    existing_backlog = list(parent.trio_backlog or [])

    system = _ITERATE_SYSTEM_PROMPT
    user_prompt = _build_iterate_user_prompt(
        task=parent,
        design_md=design_md,
        pr_diff=pr_diff,
        existing_backlog=existing_backlog,
        feedback=iteration_context.get("feedback", ""),
    )

    agent_loop = create_architect_agent(workspace=workspace, parent=parent)
    result = await agent_loop.run(system=system, prompt=user_prompt)
    output = _result_output(result)

    # The architect emits {"decision": {"action": "submit_backlog",
    # "backlog": [...]}}. Append new items to the existing backlog.
    new_items = _extract_backlog(output)
    if not new_items:
        return  # No-op iteration; caller transitions back to AWAITING_REVIEW.

    appended = existing_backlog + new_items
    async with async_session() as s:
        live = (await s.execute(select(Task).where(Task.id == parent_id))).scalar_one()
        live.trio_backlog = appended
        await s.commit()
```

Add the two helper constants at the module level near the other architect prompts:

```python
_ITERATE_SYSTEM_PROMPT = """You are the architect re-iterating on a
complex_large task in response to user feedback on its integration PR.

You receive:
  - The original task description.
  - The current ``.auto-agent/design.md``.
  - The PR diff (everything shipped so far).
  - The existing trio_backlog (some items will be marked ``done``).
  - The user's new feedback.

Your job: emit a fresh backlog of new work items that address the
feedback. Use ``submit_backlog`` with the same shape as the initial
run. If an already-done item needs to be re-done, add it as a NEW item
with a fresh id (e.g. ``S2.r1``) and ``status: pending``. Do not
mutate or remove existing items — the audit trail of what shipped
originally must survive.

If the feedback can be answered without code changes (e.g. the user
asks a question), emit an empty backlog and the loop will no-op.
"""


def _build_iterate_user_prompt(
    *, task, design_md: str, pr_diff: str, existing_backlog: list[dict], feedback: str,
) -> str:
    backlog_preview = "\n".join(
        f"- {item.get('id')}: {item.get('title')} [{item.get('status', '?')}]"
        for item in existing_backlog
    )
    return (
        f"# Task\n\n{task.title}\n\n{task.description}\n\n"
        f"# Existing design\n\n{design_md}\n\n"
        f"# What shipped so far (diff)\n\n```diff\n{pr_diff}\n```\n\n"
        f"# Existing backlog\n\n{backlog_preview or '(empty)'}\n\n"
        f"# User feedback\n\n{feedback}\n"
    )
```

Add the small read helpers near other workspace helpers in the same file:

```python
def _read_design_md(workspace_root: str, task_id: int) -> str:
    from agent.lifecycle.workspace_paths import DESIGN_PATH
    import os
    path = os.path.join(workspace_root, DESIGN_PATH)
    try:
        with open(path) as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


async def _read_pr_diff(workspace_root: str, parent) -> str:
    """Diff the integration branch against the repo's default branch."""
    from agent import sh
    from agent.lifecycle.trio.integration_branch import resolve_integration_branch

    integration_branch = resolve_integration_branch(parent)
    base = parent.repo.default_branch if parent.repo else "main"
    res = await sh.run(
        ["git", "diff", f"origin/{base}...{integration_branch}"],
        cwd=workspace_root, timeout=60,
    )
    return (res.stdout or "")[:50000]  # Truncate to keep the prompt sane.
```

And reuse the existing `_extract_backlog` (or whatever the run_initial code calls — `grep -n "submit_backlog\|extract_backlog\|backlog.*=.*decision" agent/lifecycle/trio/architect.py` will show its name). If `run_initial` calls `_extract_backlog(output)` then reuse it directly. If it inlines the parse, factor a small helper out before this task lands.

- [ ] **Step 4: Run test to verify it passes**

Run: `DATABASE_URL=$DATABASE_URL .venv/bin/python3 -m pytest tests/test_iteration_phase_architect_iterate.py -q`
Expected: `1 passed` (or `1 skipped`).

- [ ] **Step 5: Commit**

```bash
git add agent/lifecycle/trio/architect.py tests/test_iteration_phase_architect_iterate.py
git commit -m "feat(adr-017): architect.iterate entrypoint — append-not-replace backlog"
```

---

### Task 7: `run_trio_parent` accepts `iteration_context`

**Files:**
- Modify: `agent/lifecycle/trio/__init__.py` — `run_trio_parent` signature + entry branch
- Test: `tests/test_iteration_phase_dispatcher.py`

- [ ] **Step 1: Write the failing test**

```python
"""ADR-017 — run_trio_parent(iteration_context=…) routes to architect.iterate
and then proceeds through the existing per-item loop. After the loop, it
transitions ITERATING → AWAITING_REVIEW and publishes task_iteration_complete."""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from shared.events import TaskEventType


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_run_trio_parent_iteration_routes_to_iterate_and_closes_loop(
    session, publisher,
):
    from agent.lifecycle.trio import architect, run_trio_parent
    from shared.models import (
        Organization,
        Repo,
        Task,
        TaskComplexity,
        TaskSource,
        TaskStatus,
    )
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"r-{suffix}", slug=f"r-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(
        name=f"r-{suffix}", url="https://github.com/o/r.git",
        organization_id=org.id, default_branch="main",
    )
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.ITERATING,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        # Empty backlog so the per-item loop is a no-op.
        trio_backlog=[],
    )
    session.add(parent)
    await session.commit()

    iterate_mock = AsyncMock()
    with patch.object(architect, "iterate", iterate_mock):
        await run_trio_parent(
            parent,
            iteration_context={"feedback": "tweak it", "pr_url": "https://x/pr/1"},
        )

    iterate_mock.assert_awaited_once()
    assert iterate_mock.await_args.args[0] == parent.id
    # After the (no-op) per-item loop, status is AWAITING_REVIEW + event fired.
    await session.refresh(parent)
    assert parent.status == TaskStatus.AWAITING_REVIEW
    assert any(
        e.type == TaskEventType.ITERATION_COMPLETE and e.task_id == parent.id
        for e in publisher.events
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `DATABASE_URL=$DATABASE_URL .venv/bin/python3 -m pytest tests/test_iteration_phase_dispatcher.py -q`
Expected: FAIL — `run_trio_parent` doesn't accept `iteration_context`.

- [ ] **Step 3: Modify `run_trio_parent`**

In `agent/lifecycle/trio/__init__.py`, find `async def run_trio_parent(parent, *, repair_context: dict | None = None) -> None:`. Update the signature:

```python
async def run_trio_parent(
    parent: Task,
    *,
    repair_context: dict | None = None,
    iteration_context: dict | None = None,
) -> None:
```

Replace the existing entry branch:

```python
    if repair_context is not None:
        await _set_trio_phase(parent.id, TrioPhase.ARCHITECT_CHECKPOINT)
        await architect.checkpoint(parent.id, repair_context=repair_context)
    else:
        gate_ok = await _advance_through_design_gate(parent)
        if not gate_ok:
            return
```

with:

```python
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
```

Then find the existing per-item-loop tail (after `while True:` ends) where the dispatcher decides what to do next. After all items are done — when `pending` is empty and the loop `break`s — add the iteration-mode close:

```python
    # ADR-017 — iteration cycle finished. No new PR; the additive commits
    # are already on the integration branch. Transition back to
    # AWAITING_REVIEW and announce completion.
    if iteration_context is not None:
        from shared.events import task_iteration_complete
        async with async_session() as s:
            p = (await s.execute(select(Task).where(Task.id == parent.id))).scalar_one()
            await transition(
                s, p, TaskStatus.AWAITING_REVIEW,
                message="trio: iteration complete — PR updated with new commits",
            )
            await s.commit()
        await publish(task_iteration_complete(
            parent.id, summary="updated PR with your changes",
        ))
        return  # Skip the regular run_initial-style final-PR path.
```

Make sure this branches BEFORE the existing call that opens the integration PR for first-time runs. (In iteration mode the PR already exists, so we must skip `_open_integration_pr_and_transition`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `DATABASE_URL=$DATABASE_URL .venv/bin/python3 -m pytest tests/test_iteration_phase_dispatcher.py -q`
Expected: `1 passed` (or `1 skipped`).

- [ ] **Step 5: Verify state-machine accepts ITERATING → AWAITING_REVIEW**

`grep` the state_machine for the transitions map and add the edge if missing.

- [ ] **Step 6: Commit**

```bash
git add agent/lifecycle/trio/__init__.py tests/test_iteration_phase_dispatcher.py
git commit -m "feat(adr-017): run_trio_parent iteration_context branch + close loop"
```

---

### Task 8: `iteration.handle_iteration_feedback` entry point

**Files:**
- Create: `agent/lifecycle/trio/iteration.py`
- Test: `tests/test_iteration_phase_handle_feedback.py`

- [ ] **Step 1: Write the failing test**

```python
"""ADR-017 — handle_iteration_feedback transitions AWAITING_REVIEW →
ITERATING and re-enters run_trio_parent with iteration_context. Concurrent
feedback while ITERATING gets pushed to the task channel as guidance."""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_handle_feedback_transitions_and_dispatches(session, task_channel):
    from agent.lifecycle.trio import iteration
    from shared.models import (
        Organization,
        Repo,
        Task,
        TaskComplexity,
        TaskSource,
        TaskStatus,
    )
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"r-{suffix}", slug=f"r-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(
        name=f"r-{suffix}", url="https://github.com/o/r.git",
        organization_id=org.id, default_branch="main",
    )
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_REVIEW,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        pr_url="https://github.com/o/r/pull/1",
    )
    session.add(parent)
    await session.commit()

    run_mock = AsyncMock()
    with patch("agent.lifecycle.trio.iteration.run_trio_parent", run_mock):
        await iteration.handle_iteration_feedback(parent.id, "break it down further")

    await session.refresh(parent)
    assert parent.status == TaskStatus.ITERATING
    run_mock.assert_awaited_once()
    kwargs = run_mock.await_args.kwargs
    assert kwargs["iteration_context"]["feedback"] == "break it down further"
    assert kwargs["iteration_context"]["pr_url"] == "https://github.com/o/r/pull/1"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_handle_feedback_during_iteration_pushes_guidance(session, task_channel):
    from agent.lifecycle.trio import iteration
    from shared.models import (
        Organization,
        Repo,
        Task,
        TaskComplexity,
        TaskSource,
        TaskStatus,
    )
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"r-{suffix}", slug=f"r-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(
        name=f"r-{suffix}", url="https://github.com/o/r.git",
        organization_id=org.id, default_branch="main",
    )
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.ITERATING,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        pr_url="https://github.com/o/r/pull/1",
    )
    session.add(parent)
    await session.commit()

    # Mark the task as actively iterating in the in-memory guard so the
    # second feedback message takes the push-guidance branch.
    iteration._active_iteration_tasks.add(parent.id)
    try:
        run_mock = AsyncMock()
        with patch("agent.lifecycle.trio.iteration.run_trio_parent", run_mock):
            await iteration.handle_iteration_feedback(parent.id, "also do X")
    finally:
        iteration._active_iteration_tasks.discard(parent.id)

    run_mock.assert_not_awaited()  # No second dispatch.
    # Guidance was pushed to the in-memory channel.
    queued = await task_channel.channel(parent.id).pop_guidance()
    assert queued == "also do X"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `DATABASE_URL=$DATABASE_URL .venv/bin/python3 -m pytest tests/test_iteration_phase_handle_feedback.py -q`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Create `agent/lifecycle/trio/iteration.py`**

```python
"""Trio iteration phase entry point — ADR-017.

User feedback on a complex_large task whose integration PR is open
(``AWAITING_REVIEW``) lands here. The handler transitions the task to
``ITERATING`` and re-enters :func:`run_trio_parent` with an
``iteration_context`` carrying the feedback and PR URL.

Re-entrant feedback (a second message arriving while ITERATING is still
running) does NOT start a second dispatch. Instead the message is pushed
onto the per-task guidance channel; the running architect / builder
picks it up between turns. Mirrors the ``_active_clarification_tasks``
pattern in :mod:`agent.lifecycle.conversation`.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from agent.lifecycle.trio import run_trio_parent
from shared.database import async_session
from shared.models import Task, TaskStatus
from shared.task_channel import task_channel

log = logging.getLogger(__name__)

# Module-level set of task IDs currently in the iteration loop. Guards
# against re-entrant dispatch when feedback arrives while a previous
# iteration is still running.
_active_iteration_tasks: set[int] = set()


async def handle_iteration_feedback(task_id: int, message: str) -> None:
    """ADR-017 entry — user feedback on a complex_large task's PR."""
    if task_id in _active_iteration_tasks:
        log.info("iteration.busy.push_guidance", extra={"task_id": task_id})
        await task_channel(task_id).push_guidance(message)
        return

    async with async_session() as s:
        task = (
            await s.execute(select(Task).where(Task.id == task_id))
        ).scalar_one_or_none()
        if task is None:
            return
        # Refetch-and-bail: if the merge webhook already won, do nothing.
        if task.status == TaskStatus.DONE:
            return
        if task.status not in (TaskStatus.AWAITING_REVIEW, TaskStatus.ITERATING):
            log.info(
                "iteration.wrong_status.dropped",
                extra={"task_id": task_id, "status": task.status.value},
            )
            return
        from orchestrator.state_machine import transition
        await transition(
            s, task, TaskStatus.ITERATING, message="trio: iterating on user feedback",
        )
        await s.commit()
        pr_url = task.pr_url or ""

    iteration_context = {"feedback": message, "pr_url": pr_url}
    _active_iteration_tasks.add(task_id)
    try:
        async with async_session() as s:
            parent = (
                await s.execute(select(Task).where(Task.id == task_id))
            ).scalar_one()
        await run_trio_parent(parent, iteration_context=iteration_context)
    finally:
        _active_iteration_tasks.discard(task_id)
```

(Tweak the `transition` import to match where it lives — `grep -rn "def transition\b" orchestrator shared` to find it; in some versions it's `orchestrator.state_machine.transition`, in others a method on the model.)

- [ ] **Step 4: Run test to verify it passes**

Run: `DATABASE_URL=$DATABASE_URL .venv/bin/python3 -m pytest tests/test_iteration_phase_handle_feedback.py -q`
Expected: `2 passed` (or `2 skipped`).

- [ ] **Step 5: Commit**

```bash
git add agent/lifecycle/trio/iteration.py tests/test_iteration_phase_handle_feedback.py
git commit -m "feat(adr-017): iteration.handle_iteration_feedback entry + guard"
```

---

### Task 9: `route_human_message` branch for trio iteration

**Files:**
- Modify: `agent/lifecycle/conversation.py` — `route_human_message`
- Test: `tests/test_iteration_phase_routing.py`

- [ ] **Step 1: Write the failing test**

```python
"""ADR-017 — route_human_message dispatches AWAITING_REVIEW / ITERATING
complex_large tasks to iteration.handle_iteration_feedback. Non-trio
PR_CREATED tasks still go to the legacy handle_pr_review_comments."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import pytest

from shared.events import Event, HumanEventType


@pytest.mark.asyncio
async def test_routes_complex_large_awaiting_review_to_iteration():
    from agent.lifecycle import conversation

    fake_task = SimpleNamespace(
        id=5, status="awaiting_review", complexity="complex_large",
        pr_url="https://x/pr/1",
    )
    iteration_mock = AsyncMock()
    with (
        patch.object(conversation, "get_task", AsyncMock(return_value=fake_task)),
        patch(
            "agent.lifecycle.trio.iteration.handle_iteration_feedback",
            iteration_mock,
        ),
    ):
        await conversation.route_human_message(Event(
            type=HumanEventType.MESSAGE,
            task_id=5,
            payload={"message": "break it down further"},
        ))

    iteration_mock.assert_awaited_once_with(5, "break it down further")


@pytest.mark.asyncio
async def test_routes_complex_large_iterating_to_iteration():
    from agent.lifecycle import conversation

    fake_task = SimpleNamespace(
        id=5, status="iterating", complexity="complex_large",
        pr_url="https://x/pr/1",
    )
    iteration_mock = AsyncMock()
    with (
        patch.object(conversation, "get_task", AsyncMock(return_value=fake_task)),
        patch(
            "agent.lifecycle.trio.iteration.handle_iteration_feedback",
            iteration_mock,
        ),
    ):
        await conversation.route_human_message(Event(
            type=HumanEventType.MESSAGE,
            task_id=5,
            payload={"message": "also do Y"},
        ))

    iteration_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_routes_non_trio_pr_created_to_legacy_handler():
    """Non-trio (simple/complex) PR_CREATED tasks keep the existing
    handle_pr_review_comments path — that flow has its own iteration."""
    from agent.lifecycle import conversation

    fake_task = SimpleNamespace(
        id=7, status="pr_created", complexity="complex",
        pr_url="https://x/pr/2",
    )
    legacy_mock = AsyncMock()
    iter_mock = AsyncMock()
    with (
        patch.object(conversation, "get_task", AsyncMock(return_value=fake_task)),
        patch.object(conversation.review, "handle_pr_review_comments", legacy_mock),
        patch(
            "agent.lifecycle.trio.iteration.handle_iteration_feedback",
            iter_mock,
        ),
    ):
        await conversation.route_human_message(Event(
            type=HumanEventType.MESSAGE,
            task_id=7,
            payload={"message": "fix the indentation"},
        ))

    legacy_mock.assert_awaited_once()
    iter_mock.assert_not_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_iteration_phase_routing.py -q`
Expected: 2 FAIL (iteration tests — wrong handler called); 1 PASS (legacy still works).

- [ ] **Step 3: Modify `route_human_message`**

In `agent/lifecycle/conversation.py`, find the status switch in `route_human_message`. The existing block:

```python
elif task.status in ("pr_created", "awaiting_ci", "awaiting_review", "coding") and task.pr_url:
    await review.handle_pr_review_comments(task_id, comments)
```

needs to branch:

```python
elif task.status in ("awaiting_review", "iterating") and _is_trio(task):
    from agent.lifecycle.trio import iteration
    await iteration.handle_iteration_feedback(task_id, comments)
elif task.status in ("pr_created", "awaiting_ci", "awaiting_review", "coding") and task.pr_url:
    # Non-trio path — existing PR-review-comment iteration.
    await review.handle_pr_review_comments(task_id, comments)
```

Add at module level (near the top of `conversation.py`):

```python
def _is_trio(task) -> bool:
    """True for complex_large tasks driven by the trio dispatcher (ADR-013)."""
    complexity = getattr(task, "complexity", None)
    # Accept either Enum or string form (TaskData vs ORM).
    if hasattr(complexity, "value"):
        complexity = complexity.value
    return complexity == "complex_large"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_iteration_phase_routing.py -q`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add agent/lifecycle/conversation.py tests/test_iteration_phase_routing.py
git commit -m "feat(adr-017): route_human_message branches trio AWAITING_REVIEW/ITERATING to iteration"
```

---

### Task 10: Broaden `handle_feedback_event` to re-emit `human.message`

**Files:**
- Modify: `agent/lifecycle/conversation.py` — `handle_feedback_event`
- Test: `tests/test_clarification_inbound.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_clarification_inbound.py`:

```python
@pytest.mark.asyncio
async def test_handle_feedback_event_reemits_human_message_for_awaiting_review():
    """ADR-017 — Slack/Telegram thread reply on a trio task in AWAITING_REVIEW
    must re-emit as human.message so route_human_message dispatches it to
    the iteration handler. (For AWAITING_CLARIFICATION the existing path
    still calls handle_clarification_inbound.)"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from agent.lifecycle.conversation import handle_feedback_event
    from shared.events import Event, HumanEventType, TaskEventType

    fake_task = SimpleNamespace(
        id=5, status="awaiting_review", complexity="complex_large",
    )
    publish_mock = AsyncMock()
    with (
        patch("agent.lifecycle.conversation.get_task",
              AsyncMock(return_value=fake_task)),
        patch("agent.lifecycle.conversation.publish", publish_mock),
    ):
        await handle_feedback_event(Event(
            type=TaskEventType.FEEDBACK,
            task_id=5,
            payload={"message_id": 1, "sender": "slack:alan",
                     "content": "make it smaller"},
        ))

    publish_mock.assert_awaited_once()
    emitted = publish_mock.await_args.args[0]
    assert emitted.type == HumanEventType.MESSAGE
    assert emitted.task_id == 5
    assert emitted.payload["message"] == "make it smaller"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_clarification_inbound.py::test_handle_feedback_event_reemits_human_message_for_awaiting_review -q`
Expected: FAIL — publish not called (handler only routes to clarification today).

- [ ] **Step 3: Modify `handle_feedback_event`**

Currently:

```python
async def handle_feedback_event(event: Event) -> None:
    if not event.task_id:
        return
    content = event.payload.get("content", "") if event.payload else ""
    if content:
        await handle_clarification_inbound(event.task_id, content)
```

Replace with:

```python
async def handle_feedback_event(event: Event) -> None:
    """EventBus entry for ``task.feedback``.

    Fired by ``POST /tasks/{id}/messages`` whenever a user replies via
    Slack thread or Telegram reply-to. Routing:
      * AWAITING_CLARIFICATION → ``handle_clarification_inbound`` (grill /
        clarification resume).
      * AWAITING_REVIEW or ITERATING → re-emit as ``human.message`` so
        route_human_message can dispatch to trio iteration (ADR-017).
      * Anything else → drop (logged in handle_clarification_inbound).
    """
    if not event.task_id:
        return
    content = event.payload.get("content", "") if event.payload else ""
    if not content:
        return

    task = await get_task(event.task_id)
    status = getattr(task, "status", None)
    if hasattr(status, "value"):
        status = status.value

    if status in ("awaiting_review", "iterating"):
        # Re-emit so the existing route_human_message → iteration path picks it up.
        sender = event.payload.get("sender", "")
        source = sender.split(":", 1)[0] if ":" in sender else "thread"
        await publish(human_message(task_id=event.task_id, message=content, source=source))
        return

    # Fallback: clarification grill / inbound. The handler guards on
    # AWAITING_CLARIFICATION internally so non-clarification tasks no-op.
    await handle_clarification_inbound(event.task_id, content)
```

Make sure `human_message` and `publish` are imported at the top of `conversation.py` (they are already).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_clarification_inbound.py -q`
Expected: all green (including the new test).

- [ ] **Step 5: Commit**

```bash
git add agent/lifecycle/conversation.py tests/test_clarification_inbound.py
git commit -m "feat(adr-017): handle_feedback_event re-emits human.message for trio iteration"
```

---

### Task 11: Slack + Telegram notifier — `task.iteration_complete`

**Files:**
- Modify: `integrations/slack/main.py` — add `_fmt_task_iteration_complete` + dispatcher entry
- Modify: `integrations/telegram/main.py` — same shape
- Test: `tests/test_telegram_dispatcher.py` (extend) + Slack-side check

- [ ] **Step 1: Write the failing test**

Append to `tests/test_telegram_dispatcher.py`:

```python
def test_iteration_complete_renders_in_telegram():
    from integrations.telegram.main import _NOTIFICATION_FORMATTERS
    from shared.events import TaskEventType

    assert TaskEventType.ITERATION_COMPLETE in _NOTIFICATION_FORMATTERS
    fmt = _NOTIFICATION_FORMATTERS[TaskEventType.ITERATION_COMPLETE]
    msg = fmt({"summary": "updated PR with your changes"}, "task info", False, 42)
    assert "updated PR" in msg
```

And similarly in a new file `tests/test_slack_dispatcher_iteration.py`:

```python
def test_iteration_complete_renders_in_slack():
    # See test_slack_multi_team_routing.py for the import pattern.
    import sys
    from types import ModuleType
    from unittest.mock import MagicMock

    class _AiohttpStub(ModuleType):
        def __getattr__(self, name):
            obj = MagicMock()
            setattr(self, name, obj)
            return obj

    sys.modules["aiohttp"] = _AiohttpStub("aiohttp")
    for name in ("FormData", "BasicAuth", "ClientSession", "web", "TCPConnector"):
        setattr(sys.modules["aiohttp"], name, MagicMock())

    from integrations.slack.main import _NOTIFICATION_FORMATTERS
    from shared.events import TaskEventType

    assert TaskEventType.ITERATION_COMPLETE in _NOTIFICATION_FORMATTERS
    fmt = _NOTIFICATION_FORMATTERS[TaskEventType.ITERATION_COMPLETE]
    msg = fmt({"summary": "updated PR with your changes"}, "task info", False, 42)
    assert "updated PR" in msg.lower() or "iteration" in msg.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/test_telegram_dispatcher.py tests/test_slack_dispatcher_iteration.py -q`
Expected: both FAIL — `TaskEventType.ITERATION_COMPLETE not in _NOTIFICATION_FORMATTERS`.

- [ ] **Step 3: Add the Slack formatter**

In `integrations/slack/main.py`, find the existing `_fmt_task_pr_created` formatter and add a sibling:

```python
def _fmt_task_iteration_complete(p, info, _ff, tid):
    summary = (p.get("summary") or "updated PR with your changes").strip()
    url = _gate_task_url(tid)
    return (
        f"✅ *Iteration complete*\n{info}\n{summary}\n\n"
        f"PR updated with new commits. Open the task: {url}"
    )
```

Then register it in `_NOTIFICATION_FORMATTERS`:

```python
    TaskEventType.ITERATION_COMPLETE: _fmt_task_iteration_complete,
```

- [ ] **Step 4: Add the Telegram formatter**

In `integrations/telegram/main.py`, mirror — find `_fmt_task_pr_created`, add sibling:

```python
def _fmt_task_iteration_complete(p, info, _ff, _tid):
    summary = (p.get("summary") or "updated PR with your changes").strip()
    return f"✅ *Iteration complete*\n{info}\n{summary}"
```

Then register in `_NOTIFICATION_FORMATTERS`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/test_telegram_dispatcher.py tests/test_slack_dispatcher_iteration.py -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add integrations/slack/main.py integrations/telegram/main.py tests/test_telegram_dispatcher.py tests/test_slack_dispatcher_iteration.py
git commit -m "feat(adr-017): Slack + Telegram notifier for task.iteration_complete"
```

---

### Task 12: Audit and update consumers that key off `PR_CREATED`

This is a read-with-scattered-writes pass. The idea is: anywhere code currently treats `PR_CREATED` as "the long-lived state where the PR is open", it should now also accept `AWAITING_REVIEW` (and possibly `ITERATING`) so the new state machine doesn't quietly break old callers.

**Files (touch only what your grep flags):**
- `run.py` — CI poller status guards (lines around 933, 952, 955, 990, 1070, 1810)
- `agent/lifecycle/conversation.py` — review handler may still want to fire for non-trio AWAITING_REVIEW (already updated in Task 9)
- Anywhere else `grep -rn "PR_CREATED\b" --include='*.py'` flags

- [ ] **Step 1: Run the audit grep**

```bash
grep -rn "TaskStatus.PR_CREATED\b\|\"pr_created\"\|'pr_created'" run.py agent orchestrator shared integrations --include="*.py" | grep -v test_ | grep -v __pycache__
```

For each hit, decide:
  - **Keep PR_CREATED only** — the line is specifically about the PR-just-opened *moment* (e.g. the notifier formatter). No change.
  - **Expand to also accept AWAITING_REVIEW / ITERATING** — the line is treating PR_CREATED as "PR is open" semantics. Add the new statuses to the set.

- [ ] **Step 2: Update the CI / review poll status sets**

In `run.py` line 1810: `REVIEW_POLL_STATUSES = {TaskStatus.AWAITING_REVIEW, TaskStatus.AWAITING_CI, TaskStatus.PR_CREATED}` — already includes both. Verify it also catches `ITERATING` if the CI poller should run during iteration (it probably shouldn't — the iteration's own smoke agent covers correctness). Skip if uncertain.

In `run.py` lines 933 / 1070 (status guards in PR / merge handlers): if they're checking "is the PR still in an open state", expand to `(PR_CREATED, AWAITING_REVIEW, ITERATING)`.

- [ ] **Step 3: Update the merge-webhook handler**

`grep -rn "merge\|task_done\|PR_MERGED\|pull_request" orchestrator/webhooks/github.py | head -30` — find where a merged PR moves the task to DONE. Make sure the source-status check accepts `AWAITING_REVIEW` and `ITERATING` (not just `PR_CREATED`). Without this, merging during iteration could fail to transition.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: same baseline failure count as before this branch (`43 failed` is the documented baseline; anything more is a regression).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore(adr-017): expand PR-open status guards to cover AWAITING_REVIEW/ITERATING"
```

---

### Task 13: End-to-end smoke

**Files:**
- Test: `tests/test_iteration_phase_end_to_end.py`

This is the integration check — it threads a feedback message through the full pipeline (web-message endpoint → human.message → route_human_message → iteration.handle_iteration_feedback → run_trio_parent → architect.iterate). The architect call is mocked because we don't want to actually drive an LLM in CI.

- [ ] **Step 1: Write the integration test**

```python
"""ADR-017 — end-to-end: web UI message on an AWAITING_REVIEW trio task
threads through to architect.iterate and back to AWAITING_REVIEW with the
iteration-complete event published."""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from shared.events import HumanEventType, TaskEventType


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_feedback_flows_from_web_to_iteration_complete(session, publisher):
    from agent.lifecycle import conversation
    from agent.lifecycle.trio import architect
    from orchestrator.router import add_task_message
    from shared.events import Event
    from shared.models import (
        Organization,
        Repo,
        Task,
        TaskComplexity,
        TaskSource,
        TaskStatus,
    )
    from shared.types import TaskMessageRequest
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"e2e-{suffix}", slug=f"e2e-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(
        name=f"r-{suffix}", url="https://github.com/o/r.git",
        organization_id=org.id, default_branch="main",
    )
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_REVIEW,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        pr_url="https://github.com/o/r/pull/1",
        trio_backlog=[],
    )
    session.add(parent)
    await session.commit()

    # 1. POST /tasks/{id}/message publishes human.message.
    with patch("orchestrator.router._get_task_in_org", return_value=parent):
        await add_task_message(
            task_id=parent.id,
            req=TaskMessageRequest(message="break it down further", username="alan"),
            session=session,
            org_id=org.id,
        )

    # 2. Simulate the bus dispatching human.message to route_human_message.
    iterate_mock = AsyncMock()
    with patch.object(architect, "iterate", iterate_mock):
        for ev in list(publisher.events):
            if ev.type == HumanEventType.MESSAGE and ev.task_id == parent.id:
                await conversation.route_human_message(ev)

    iterate_mock.assert_awaited()
    await session.refresh(parent)
    assert parent.status == TaskStatus.AWAITING_REVIEW, (
        f"end-state should be AWAITING_REVIEW, got {parent.status}"
    )
    assert any(
        e.type == TaskEventType.ITERATION_COMPLETE for e in publisher.events
    )
```

- [ ] **Step 2: Run it**

Run: `DATABASE_URL=$DATABASE_URL .venv/bin/python3 -m pytest tests/test_iteration_phase_end_to_end.py -q`
Expected: `1 passed` (or `1 skipped` without DATABASE_URL).

- [ ] **Step 3: Lint + format + full suite**

```bash
.venv/bin/python3 -m ruff check .
.venv/bin/python3 -m ruff format --check .
.venv/bin/python3 -m pytest tests/ -q
```

Expected: lint clean on touched files, baseline (43 failures) unchanged.

- [ ] **Step 4: Commit**

```bash
git add tests/test_iteration_phase_end_to_end.py
git commit -m "test(adr-017): end-to-end iteration phase smoke"
```

---

### Task 14: Deploy + verify on the VM

- [ ] **Step 1: Deploy**

Run: `./scripts/deploy.sh`
Expected: containers recreate, alembic upgrade lands, `{"status":"ok"}`.

- [ ] **Step 2: Verify migration on the VM**

```bash
ssh azureuser@172.190.26.82 'cd ~/auto-agent && docker compose exec -T postgres psql -U autoagent -d autoagent -c "SELECT version_num FROM alembic_version;"'
```

Expected: `045`.

```bash
ssh azureuser@172.190.26.82 'cd ~/auto-agent && docker compose exec -T postgres psql -U autoagent -d autoagent -c "SELECT unnest(enum_range(NULL::taskstatus)) WHERE unnest::text IN ('"'"'ITERATING'"'"', '"'"'iterating'"'"');"'
```

Expected: at least one row.

- [ ] **Step 3: Live test on task 5 (or whichever task is currently in AWAITING_REVIEW)**

Post feedback via the web UI chat on the task. Watch logs:

```bash
ssh azureuser@172.190.26.82 'docker compose -f ~/auto-agent/docker-compose.yml logs auto-agent -f --tail=0 2>&1 | grep --line-buffered -E "iteration|ITERATING|architect.iterate"'
```

Expected line sequence (over the course of an iteration):
  1. `iteration.handle_iteration_feedback` entry
  2. status transition AWAITING_REVIEW → ITERATING
  3. trio.parent.set_phase ARCHITECT_ITERATING
  4. architect.iterate.complete
  5. per-item dispatcher events for any new backlog items
  6. status transition ITERATING → AWAITING_REVIEW
  7. task.iteration_complete event

- [ ] **Step 4: Save to team memory**

After verifying live, run a `mcp__team-memory__remember` for the entity "Auto-Agent trio iteration phase" with kind=architecture, capturing the key invariants from the ADR (PR_CREATED is single-fire; AWAITING_REVIEW is the long-lived state; ITERATING re-enters via iteration_context; append-not-replace backlog; no force-push).

---

## Self-review notes

Spec coverage:
- ✅ State machine — Tasks 1, 2, 5, 7
- ✅ Channel adapters (web, Slack/Telegram, GitHub) — Tasks 4, 10 (Slack/Telegram thread-reply); GitHub PR comments already emit human.message (verified by audit in Task 12)
- ✅ Routing — Task 9
- ✅ Iteration module — Task 8
- ✅ Architect.iterate — Task 6
- ✅ Dispatcher kwarg + tail — Task 7
- ✅ Notifier — Task 11
- ✅ Audit — Task 12
- ⚠️ web-next UI rendering of `ITERATING` / `AWAITING_REVIEW` — explicitly DEFERRED (the ADR mentions it; this plan is backend-only). UI work goes into a separate plan.

Placeholder scan: none. Every step has either exact code or an exact command.

Type consistency: `iteration_context` shape `{feedback: str, pr_url: str}` consistent across Tasks 6, 7, 8, 13. `_active_iteration_tasks` set defined in Task 8 only.
