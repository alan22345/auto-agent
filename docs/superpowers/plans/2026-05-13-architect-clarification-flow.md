# Architect Clarification Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the trio architect a structured `awaiting_clarification` path so it can ask product-shaped questions instead of stalling. In freeform mode a new PO agent answers automatically (using a new `Repo.product_brief` context blob); in non-freeform mode the existing user-clarification channels surface the question. The architect's `AgentLoop` session is persisted across the wait and resumed turn-by-turn when the answer lands.

**Architecture:** Architect emits `{"decision":{"action":"awaiting_clarification","question":"..."}}`. `architect.py` persists the AgentLoop session via the existing `agent.session.Session` class to a JSON blob in the workspace, writes the question to a new `ArchitectAttempt` row, transitions parent `TRIO_EXECUTING → AWAITING_CLARIFICATION` (trio_phase stays set as a disambiguator), publishes `ARCHITECT_CLARIFICATION_NEEDED`. The dispatcher routes to `po_agent.answer_architect_question` (freeform) or republishes the existing `task.clarification_needed` event (non-freeform, existing integrations format it). Answer lands via either `po_agent` writing directly or the user's reply through `handle_clarification_inbound`. `ARCHITECT_CLARIFICATION_RESOLVED` fires; parent transitions back to `TRIO_EXECUTING`; `architect.resume()` reloads the session, injects answer as a user message, continues the AgentLoop.

**Tech Stack:** Python 3.12 (async), SQLAlchemy 2.0 async, Alembic, FastAPI, Next.js 14 App Router + TanStack Query, Pydantic v2.

**Spec:** `docs/superpowers/specs/2026-05-13-architect-clarification-flow-design.md`.

---

## Phase 1: Schema & Foundation

### Task 1: Add the new ORM columns

**Files:**
- Modify: `shared/models/core.py` (`Repo`)
- Modify: `shared/models/trio.py` (`ArchitectAttempt`)
- Test: `tests/test_trio_models_migration.py` (extend)

- [ ] **Step 1: Add the failing test**

Append to `tests/test_trio_models_migration.py`:

```python
@pytest.mark.asyncio
async def test_clarification_columns_exist(session: AsyncSession) -> None:
    """Migration 034 adds the clarification + product_brief columns."""
    # Repo.product_brief
    cols = (
        await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'repos' AND column_name = 'product_brief'"
        ))
    ).scalars().all()
    assert cols == ["product_brief"], "Repo.product_brief should exist"

    # ArchitectAttempt clarification columns
    cols = (
        await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'architect_attempts' "
            "AND column_name IN ('clarification_question', "
            "                    'clarification_answer', "
            "                    'clarification_source', "
            "                    'session_blob_path')"
        ))
    ).scalars().all()
    assert set(cols) == {
        "clarification_question", "clarification_answer",
        "clarification_source", "session_blob_path",
    }
```

- [ ] **Step 2: Verify the test fails**

Run: `DATABASE_URL=postgresql+asyncpg://autoagent:changeme@localhost:5432/autoagent .venv/bin/python3 -m pytest tests/test_trio_models_migration.py::test_clarification_columns_exist -v`
Expected: FAIL (columns don't exist yet — migration 034 not written) OR SKIP if `DATABASE_URL` not set.

- [ ] **Step 3: Add `product_brief` to `Repo`**

In `shared/models/core.py`, locate the `Repo` class (around line 136). Add after `harness_pr_url`:

```python
    # Product Owner context — repo-scoped, free-text markdown.
    # Describes the product mission, requirements, non-goals. Injected
    # into every PO prompt where the PO is asked to make a
    # product-shaped decision (today: architect clarification answers).
    product_brief = Column(Text, nullable=True)
```

- [ ] **Step 4: Add clarification columns to `ArchitectAttempt`**

In `shared/models/trio.py`, locate the `ArchitectAttempt` class. Add four columns after `tool_calls`:

```python
    # Set when phase=INITIAL/CHECKPOINT and decision.action=
    # "awaiting_clarification". Holds the prose question architect asked
    # and the prose answer the PO (freeform) or user (non-freeform)
    # gave. session_blob_path is the relative path under the workspace
    # tree where Session.save() persisted the AgentLoop's messages.
    clarification_question = Column(Text, nullable=True)
    clarification_answer = Column(Text, nullable=True)
    clarification_source = Column(String(16), nullable=True)  # 'user' | 'po'
    session_blob_path = Column(String(512), nullable=True)
```

- [ ] **Step 5: Commit**

```bash
git add shared/models/core.py shared/models/trio.py tests/test_trio_models_migration.py
git commit -m "feat(models): add product_brief + architect clarification columns"
```

---

### Task 2: Alembic migration 034

**Files:**
- Create: `migrations/versions/034_clarification_flow.py`

- [ ] **Step 1: Write the migration**

Create `migrations/versions/034_clarification_flow.py`:

```python
"""clarification flow: Repo.product_brief + ArchitectAttempt clarification cols

Revision ID: 034
Revises: 033
Create Date: 2026-05-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "repos",
        sa.Column("product_brief", sa.Text(), nullable=True),
    )
    op.add_column(
        "architect_attempts",
        sa.Column("clarification_question", sa.Text(), nullable=True),
    )
    op.add_column(
        "architect_attempts",
        sa.Column("clarification_answer", sa.Text(), nullable=True),
    )
    op.add_column(
        "architect_attempts",
        sa.Column("clarification_source", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "architect_attempts",
        sa.Column("session_blob_path", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("architect_attempts", "session_blob_path")
    op.drop_column("architect_attempts", "clarification_source")
    op.drop_column("architect_attempts", "clarification_answer")
    op.drop_column("architect_attempts", "clarification_question")
    op.drop_column("repos", "product_brief")
```

- [ ] **Step 2: Verify migration is well-formed**

Run: `.venv/bin/python3 -c "from migrations.versions import *" 2>&1 | head -5`
Expected: no errors.

Run: `.venv/bin/python3 -m alembic check 2>&1 | tail -5`
Expected: confirms 034 is reachable from head.

- [ ] **Step 3: Re-run the model test against a DB with 034 applied**

(Requires DB. Locally skip; verified on VM in deploy step.) On the VM (or any Postgres bound to a fresh DB):

```bash
docker compose exec -T auto-agent alembic upgrade head
docker compose exec -T postgres psql -U autoagent -d autoagent -c "\d repos" | grep product_brief
docker compose exec -T postgres psql -U autoagent -d autoagent -c "\d architect_attempts" | grep clarification
```
Expected: `product_brief | text`; four clarification columns listed.

- [ ] **Step 4: Commit**

```bash
git add migrations/versions/034_clarification_flow.py
git commit -m "feat(migrations): 034 — clarification flow columns"
```

---

### Task 3: Extend Pydantic types

**Files:**
- Modify: `shared/types.py`
- Test: `tests/test_types.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_types.py`:

```python
def test_architect_attempt_out_has_clarification_fields():
    from datetime import UTC, datetime
    from shared.types import ArchitectAttemptOut
    out = ArchitectAttemptOut(
        id=1, task_id=1, phase="initial", cycle=1,
        reasoning="r", decision=None, consult_question=None,
        consult_why=None, architecture_md_after=None,
        commit_sha=None, tool_calls=[],
        clarification_question="Q1",
        clarification_answer="A1",
        clarification_source="po",
        created_at=datetime.now(UTC),
    )
    assert out.clarification_question == "Q1"
    assert out.clarification_answer == "A1"
    assert out.clarification_source == "po"


def test_repo_data_has_product_brief():
    from shared.types import RepoData
    r = RepoData(id=1, name="x", url="https://github.com/x/y.git",
                 product_brief="# Mission\nBuild X.")
    assert r.product_brief == "# Mission\nBuild X."
```

- [ ] **Step 2: Verify failure**

Run: `.venv/bin/python3 -m pytest tests/test_types.py::test_architect_attempt_out_has_clarification_fields tests/test_types.py::test_repo_data_has_product_brief -v`
Expected: FAIL (`ValidationError: extra fields not permitted` for `clarification_question`, and `product_brief` missing on `RepoData`).

- [ ] **Step 3: Extend `ArchitectAttemptOut`**

In `shared/types.py`, locate the `ArchitectAttemptOut` class. Replace it with:

```python
class ArchitectAttemptOut(BaseModel):
    """API shape for an architect_attempts row."""
    id: int
    task_id: int
    phase: Literal["initial", "consult", "checkpoint", "revision"]
    cycle: int
    reasoning: str
    decision: dict | None = None
    consult_question: str | None = None
    consult_why: str | None = None
    architecture_md_after: str | None = None
    commit_sha: str | None = None
    tool_calls: list[dict]
    # Clarification fields (set when decision.action="awaiting_clarification").
    # session_blob_path stays internal — not exposed here.
    clarification_question: str | None = None
    clarification_answer: str | None = None
    clarification_source: Literal["user", "po"] | None = None
    created_at: datetime
```

- [ ] **Step 4: Extend `RepoData`**

In `shared/types.py`, locate the `RepoData` class and add `product_brief`:

```python
class RepoData(BaseModel):
    """Typed representation of a repo from the orchestrator API."""
    id: int
    name: str
    url: str
    default_branch: str | None = None
    summary: str | None = None
    summary_updated_at: datetime | None = None
    ci_checks: str | None = None
    harness_onboarded: bool = False
    harness_pr_url: str | None = None
    product_brief: str | None = None
```

- [ ] **Step 5: Verify the tests pass**

Run: `.venv/bin/python3 -m pytest tests/test_types.py::test_architect_attempt_out_has_clarification_fields tests/test_types.py::test_repo_data_has_product_brief -v`
Expected: PASS.

- [ ] **Step 6: Regenerate TS types**

Run: `.venv/bin/python3 scripts/gen_ts_types.py`
Expected: `Wrote /Users/.../web-next/types/api.ts (NNN lines)`. Confirm `ArchitectAttemptOut` now has the 3 clarification fields and `RepoData` has `product_brief`:

```bash
grep -A 1 "clarification_question\|product_brief" web-next/types/api.ts | head -10
```

- [ ] **Step 7: Commit**

```bash
git add shared/types.py tests/test_types.py web-next/types/api.ts
git commit -m "feat(types): clarification fields on ArchitectAttemptOut + product_brief on RepoData"
```

---

### Task 4: State machine — add the two new transitions

**Files:**
- Modify: `orchestrator/state_machine.py`
- Test: `tests/test_state_machine.py` (or create if absent — check first)

- [ ] **Step 1: Check whether a test file exists**

Run: `ls tests/test_state_machine.py tests/test_trio_state_machine.py 2>&1 | head -3`

If `test_trio_state_machine.py` doesn't exist, create it. If it does, append to it.

- [ ] **Step 2: Write the failing test**

In `tests/test_trio_state_machine.py` (create or extend):

```python
"""State machine — trio clarification transitions."""
from __future__ import annotations

from orchestrator.state_machine import TRANSITIONS
from shared.models import TaskStatus


def test_trio_executing_can_transition_to_awaiting_clarification():
    assert TaskStatus.AWAITING_CLARIFICATION in TRANSITIONS[TaskStatus.TRIO_EXECUTING]


def test_awaiting_clarification_can_transition_to_trio_executing():
    assert TaskStatus.TRIO_EXECUTING in TRANSITIONS[TaskStatus.AWAITING_CLARIFICATION]


def test_existing_transitions_unchanged():
    """Sanity: planner's clarification path is untouched."""
    assert TaskStatus.PLANNING in TRANSITIONS[TaskStatus.AWAITING_CLARIFICATION]
    assert TaskStatus.CODING in TRANSITIONS[TaskStatus.AWAITING_CLARIFICATION]
    assert TaskStatus.AWAITING_CLARIFICATION in TRANSITIONS[TaskStatus.PLANNING]
```

- [ ] **Step 3: Verify failure**

Run: `.venv/bin/python3 -m pytest tests/test_trio_state_machine.py -v`
Expected: the two new tests FAIL with AssertionError; the "unchanged" test passes.

- [ ] **Step 4: Add the transitions**

In `orchestrator/state_machine.py`, locate the `TRANSITIONS` dict. Update two entries:

```python
    TaskStatus.PLANNING: {
        TaskStatus.AWAITING_APPROVAL, TaskStatus.AWAITING_CLARIFICATION,
        TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.BLOCKED_ON_QUOTA,
    },
    # ... unchanged entries ...
    TaskStatus.AWAITING_CLARIFICATION: {
        TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.FAILED,
        TaskStatus.TRIO_EXECUTING,  # NEW — trio architect resume
    },
    # ... unchanged ...
    TaskStatus.TRIO_EXECUTING: {
        TaskStatus.PR_CREATED, TaskStatus.BLOCKED,
        TaskStatus.AWAITING_CLARIFICATION,  # NEW — architect needs answers
    },
```

- [ ] **Step 5: Verify pass**

Run: `.venv/bin/python3 -m pytest tests/test_trio_state_machine.py -v`
Expected: all three tests PASS.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/state_machine.py tests/test_trio_state_machine.py
git commit -m "feat(state-machine): trio architect clarification transitions"
```

---

## Phase 2: Event Types

### Task 5: Add the two new TaskEventType values

**Files:**
- Modify: `shared/events.py`
- Test: `tests/test_events.py` (check if exists; if not, this is verified via downstream tests)

- [ ] **Step 1: Add the values**

In `shared/events.py`, locate `class TaskEventType(StrEnum)`. Add two values at the end of the trio-related cluster (after `VERIFY_SKIPPED_NO_RUNNER` line is fine):

```python
    # Trio architect clarification flow — distinct from the planner's
    # CLARIFICATION_NEEDED. The architect publishes
    # ARCHITECT_CLARIFICATION_NEEDED so the dispatcher can route to PO
    # (freeform) or republish the planner event (non-freeform).
    # ARCHITECT_CLARIFICATION_RESOLVED fires when the answer lands.
    ARCHITECT_CLARIFICATION_NEEDED = "task.architect_clarification_needed"
    ARCHITECT_CLARIFICATION_RESOLVED = "task.architect_clarification_resolved"
```

- [ ] **Step 2: Verify enum loads**

Run: `.venv/bin/python3 -c "from shared.events import TaskEventType; print(TaskEventType.ARCHITECT_CLARIFICATION_NEEDED.value); print(TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED.value)"`
Expected:
```
task.architect_clarification_needed
task.architect_clarification_resolved
```

- [ ] **Step 3: Commit**

```bash
git add shared/events.py
git commit -m "feat(events): ARCHITECT_CLARIFICATION_NEEDED + _RESOLVED event types"
```

---

## Phase 3: Architect Emit Path

### Task 6: Update architect prompts

**Files:**
- Modify: `agent/lifecycle/trio/prompts.py`
- Test: covered indirectly by Task 7 — no standalone prompt test (prompts are strings).

- [ ] **Step 1: Update `ARCHITECT_INITIAL_SYSTEM`**

In `agent/lifecycle/trio/prompts.py`, replace the existing `ARCHITECT_INITIAL_SYSTEM` constant with:

```python
ARCHITECT_INITIAL_SYSTEM = """\
You are the architect for a complex task. Your job:

1. Produce a clear ARCHITECTURE.md at the repo root describing the app's shape:
   stack, top-level file layout, key data model, key routes/endpoints.
2. Produce a backlog of bounded work items that builders will implement one at
   a time. Each item must have a title (becomes a PR title) and a description
   (becomes a PR body and a builder prompt). Keep each item small enough that
   one builder cycle can complete it.
3. For cold-start tasks (empty workspace), scaffold the project via `bash`
   (e.g. `npx create-next-app`, `uv init`). Commit scaffolded files.
4. For non-obvious tradeoffs, call `record_decision` with a properly-formatted
   ADR. Examples: stack choice, data model decisions, ambiguous requirements.
5. For product/UX-shaped tasks, call `request_market_brief` BEFORE picking
   the stack to ground decisions in the market shape.

You have a Product Owner you can consult when a product-shaped decision
genuinely blocks the design. Use this only when (a) the answer materially
changes the architecture AND (b) you cannot reasonably default to one branch
and ship. To ask, emit your reasoning followed by:

```json
{"decision": {"action": "awaiting_clarification", "question": "..."}}
```

The `question` is a single string. Pack multiple sub-questions into it as
a numbered markdown list, each with the reason it matters. The system will
route it to the PO (freeform mode) or to the human user (otherwise), and
resume you with the answer.

DO NOT ask for clarification when:
- You could make a reasonable default and revise later.
- The answer is grep-able from the workspace.
- You're trying to dodge committing to a stack.

Tools you do NOT have: writing source code, opening PRs, running tests.
Stick to ARCHITECTURE.md, ADRs in docs/decisions/, and scaffold commands.

Output your reasoning as plain text. When you are done with this initial
pass, your last message must include EITHER a backlog JSON:

```json
{"backlog": [
  {"id": "uuid-1", "title": "Add Postgres schema for recipes",
   "description": "..."}
]}
```

OR the clarification decision shown above. Never both, never neither.
"""
```

- [ ] **Step 2: Update `ARCHITECT_CHECKPOINT_SYSTEM`**

Replace `ARCHITECT_CHECKPOINT_SYSTEM` with:

```python
ARCHITECT_CHECKPOINT_SYSTEM = """\
You are the architect, running a checkpoint after a builder child task
finished (or after the integration PR's CI failed).

Read what was just merged (`git log`, `git diff`) and current ARCHITECTURE.md.
Decide:
- `continue` — backlog still has pending items; mark the last one done and
  optionally add new items discovered while reviewing the merge.
- `revise` — the design needs to change; you will re-enter the architecting
  phase to rewrite ARCHITECTURE.md and the backlog.
- `done` — everything in the backlog is complete; the trio's job is finished.
- `awaiting_clarification` — a product-shaped question now blocks the next
  step and the system should route it to PO (freeform) or user (otherwise).
  Use only when defaulting and shipping is genuinely worse than waiting.

If you were re-entered because of a CI failure on the integration PR (the
prompt will tell you), diagnose the failure and add fix work items. The
builders will pick them up.

Output your reasoning, then end with EITHER the checkpoint JSON:

```json
{"backlog": [...updated...], "decision": {"action": "continue|revise|done", "reason": "..."}}
```

OR a clarification:

```json
{"decision": {"action": "awaiting_clarification", "question": "..."}}
```
"""
```

- [ ] **Step 3: Verify the file imports**

Run: `.venv/bin/python3 -c "from agent.lifecycle.trio.prompts import ARCHITECT_INITIAL_SYSTEM, ARCHITECT_CHECKPOINT_SYSTEM; assert 'awaiting_clarification' in ARCHITECT_INITIAL_SYSTEM; assert 'awaiting_clarification' in ARCHITECT_CHECKPOINT_SYSTEM; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add agent/lifecycle/trio/prompts.py
git commit -m "feat(trio): teach architect prompts the awaiting_clarification path"
```

---

### Task 7: Extract clarification decision helper

**Files:**
- Modify: `agent/lifecycle/trio/architect.py` (add `_extract_clarification` near `_extract_backlog`)
- Test: `tests/test_architect_extractors.py` (check if exists; if not, create)

- [ ] **Step 1: Check for existing test file**

Run: `ls tests/test_architect_extractors.py 2>&1`

If absent, you'll create it in step 2. If present, append.

- [ ] **Step 2: Write the failing test**

Create or extend `tests/test_architect_extractors.py`:

```python
"""Tests for the JSON extractors in agent/lifecycle/trio/architect.py."""
from __future__ import annotations

import pytest

from agent.lifecycle.trio.architect import (
    _extract_backlog, _extract_clarification,
)


def test_extract_clarification_returns_question_string():
    text = (
        "Some reasoning.\n\n"
        '```json\n'
        '{"decision": {"action": "awaiting_clarification", "question": "Q?"}}\n'
        '```\n'
    )
    assert _extract_clarification(text) == "Q?"


def test_extract_clarification_returns_none_when_no_block():
    assert _extract_clarification("no json here") is None


def test_extract_clarification_returns_none_when_action_is_not_awaiting():
    text = '```json\n{"decision": {"action": "done", "reason": "shipped"}}\n```'
    assert _extract_clarification(text) is None


def test_extract_clarification_returns_none_when_question_missing():
    text = '```json\n{"decision": {"action": "awaiting_clarification"}}\n```'
    assert _extract_clarification(text) is None


def test_extract_clarification_picks_last_valid_block():
    """Two JSON blocks — clarification wins if it's last and valid."""
    text = (
        '```json\n{"backlog": [{"id": "1", "title": "x", "description": "y"}]}\n```\n'
        'But actually wait,\n'
        '```json\n{"decision": {"action": "awaiting_clarification", "question": "Q?"}}\n```'
    )
    assert _extract_clarification(text) == "Q?"


def test_backlog_takes_precedence_when_clarification_absent():
    """Existing behaviour: backlog still extracts when present alone."""
    text = '```json\n{"backlog": [{"id": "1", "title": "x", "description": "y"}]}\n```'
    backlog = _extract_backlog(text)
    assert backlog is not None
    assert len(backlog) == 1
```

- [ ] **Step 3: Verify failure**

Run: `.venv/bin/python3 -m pytest tests/test_architect_extractors.py -v`
Expected: FAIL with `ImportError: cannot import name '_extract_clarification'`.

- [ ] **Step 4: Add `_extract_clarification` to architect.py**

In `agent/lifecycle/trio/architect.py`, near the existing `_extract_backlog` function (around line 83), add:

```python
def _extract_clarification(text: str) -> str | None:
    """Extract the architect's clarification question if present.

    Looks for the LAST ```json fenced block in the message whose top-level
    shape is ``{"decision": {"action": "awaiting_clarification", "question": "..."}}``.
    Returns the question string, or None if no such block exists or it's
    malformed. Returns None for blocks with action != "awaiting_clarification"
    or with no question field — callers fall through to backlog extraction.
    """
    import json
    import re

    blocks = re.findall(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    for block in reversed(blocks):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        decision = data.get("decision")
        if not isinstance(decision, dict):
            continue
        if decision.get("action") != "awaiting_clarification":
            continue
        question = decision.get("question")
        if isinstance(question, str) and question.strip():
            return question
    return None
```

- [ ] **Step 5: Verify pass**

Run: `.venv/bin/python3 -m pytest tests/test_architect_extractors.py -v`
Expected: 6 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/lifecycle/trio/architect.py tests/test_architect_extractors.py
git commit -m "feat(trio): _extract_clarification helper for architect output"
```

---

### Task 8: `run_initial` recognises `awaiting_clarification`

**Files:**
- Modify: `agent/lifecycle/trio/architect.py` (`run_initial`)
- Test: `tests/test_architect_emit_clarification.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_architect_emit_clarification.py`:

```python
"""Architect emits awaiting_clarification → session saved, state transitions,
ARCHITECT_CLARIFICATION_NEEDED published."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.events import TaskEventType


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_run_initial_handles_awaiting_clarification(session, publisher):
    """When the architect's output contains an awaiting_clarification JSON
    block, run_initial: writes the question to architect_attempts, transitions
    the parent to AWAITING_CLARIFICATION, publishes ARCHITECT_CLARIFICATION_NEEDED.
    """
    from shared.models import (
        ArchitectAttempt, Organization, Repo, Task, TaskComplexity,
        TaskSource, TaskStatus, TrioPhase,
    )

    org = Organization(name="t", slug="t")
    session.add(org)
    await session.flush()
    repo = Repo(name="r", url="https://github.com/o/r.git",
                organization_id=org.id, default_branch="main")
    session.add(repo)
    await session.flush()
    parent = Task(
        title="Parent", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING, complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.commit()

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=MagicMock(
        output=(
            "I need to know which framework to use first.\n\n"
            '```json\n'
            '{"decision": {"action": "awaiting_clarification", '
            '"question": "Pick React or Vue?"}}\n'
            '```'
        ),
        messages=[MagicMock(role="user", content="seed")],
        api_messages=[MagicMock(role="user", content="seed")],
    ))

    with patch("agent.lifecycle.trio.architect.create_architect_agent",
               return_value=fake_agent), \
         patch("agent.lifecycle.trio.architect._prepare_parent_workspace",
               AsyncMock(return_value=MagicMock(root="/tmp/ws"))), \
         patch("agent.lifecycle.trio.architect.home_dir_for_task",
               AsyncMock(return_value=None)), \
         patch("agent.session.Session.save", AsyncMock()):
        from agent.lifecycle.trio import architect
        await architect.run_initial(parent.id)

    await session.refresh(parent)
    assert parent.status == TaskStatus.AWAITING_CLARIFICATION
    assert parent.trio_phase == TrioPhase.ARCHITECTING

    # Architect attempt row stores the question, not a backlog.
    from sqlalchemy import select as _sel
    attempt = (await session.execute(
        _sel(ArchitectAttempt).where(ArchitectAttempt.task_id == parent.id)
    )).scalar_one()
    assert attempt.clarification_question == "Pick React or Vue?"
    assert attempt.clarification_answer is None
    assert attempt.session_blob_path is not None

    # Event published.
    needed = [e for e in publisher.events
              if e.type == TaskEventType.ARCHITECT_CLARIFICATION_NEEDED]
    assert len(needed) == 1
    assert needed[0].task_id == parent.id
    assert needed[0].payload["question"] == "Pick React or Vue?"
```

- [ ] **Step 2: Verify failure**

Run: `DATABASE_URL=postgresql+asyncpg://autoagent:changeme@localhost:5432/autoagent .venv/bin/python3 -m pytest tests/test_architect_emit_clarification.py -v`
Expected: FAIL (current `run_initial` doesn't check for clarification — falls through to `_extract_backlog` returning None, which goes to BLOCKED).

- [ ] **Step 3: Modify `run_initial` to check for clarification first**

In `agent/lifecycle/trio/architect.py`, locate the block after `output = _result_output(run_result)` (around line 347–350). Insert a clarification check BEFORE the existing `backlog = _extract_backlog(output)` line:

```python
    output = _result_output(run_result)
    tool_calls = _result_tool_calls(run_result, agent)

    # First: did the architect ask for clarification? If so, persist
    # session + transition state instead of trying to parse a backlog.
    clarification = _extract_clarification(output)
    if clarification is not None:
        await _emit_clarification(
            parent_task_id=parent_task_id,
            agent=agent,
            workspace=workspace,
            output=output,
            tool_calls=tool_calls,
            question=clarification,
            phase=ArchitectPhase.INITIAL,
        )
        return

    backlog = _extract_backlog(output)
```

- [ ] **Step 4: Add `_emit_clarification` helper to architect.py**

Add this helper function in `agent/lifecycle/trio/architect.py`, near the other private helpers (above `run_initial`):

```python
async def _emit_clarification(
    *,
    parent_task_id: int,
    agent,  # AgentLoop
    workspace,
    output: str,
    tool_calls: list[dict],
    question: str,
    phase: "ArchitectPhase",
) -> None:
    """Persist the AgentLoop session, write the question row, transition
    the parent to AWAITING_CLARIFICATION, publish *_NEEDED.

    Called from run_initial / checkpoint / run_revision when the architect
    output contains an awaiting_clarification JSON block.
    """
    from agent.session import Session
    from shared.events import Event, TaskEventType, publish
    from orchestrator.state_machine import transition

    # 1. Persist the AgentLoop messages + api_messages so resume() can
    #    pick up exactly where the architect left off.
    session_id = f"trio-{parent_task_id}"
    session_blob_dir = workspace.root if hasattr(workspace, "root") else str(workspace)
    file_session = Session(session_id=session_id, storage_dir=session_blob_dir)
    await file_session.save(agent.messages, agent.api_messages)
    # session_blob_path is the file Session.save() wrote — relative path
    # under the workspace dir so the architect.resume() side can locate it
    # from any reconstructed workspace path.
    session_blob_path = f"{session_id}.json"

    # 2. Loop guard — count clarification rounds in this parent's lifetime.
    async with async_session() as s:
        prior = (
            await s.execute(
                select(func.count())
                .select_from(ArchitectAttempt)
                .where(ArchitectAttempt.task_id == parent_task_id)
                .where(ArchitectAttempt.clarification_question.is_not(None))
            )
        ).scalar_one()

        cap = int(os.environ.get("TRIO_MAX_CLARIFICATIONS", "3"))
        if prior >= cap:
            log.warning(
                "architect.clarification.loop_guard",
                task_id=parent_task_id, prior_rounds=prior, cap=cap,
            )
            parent = (
                await s.execute(select(Task).where(Task.id == parent_task_id))
            ).scalar_one()
            await transition(
                s, parent, TaskStatus.BLOCKED,
                message=f"architect asked for clarification {prior + 1}x; capped at {cap}",
            )
            s.add(ArchitectAttempt(
                task_id=parent_task_id,
                phase=phase,
                cycle=_next_cycle_sync(prior + 1),
                reasoning=output,
                tool_calls=tool_calls,
                clarification_question=question,
                session_blob_path=session_blob_path,
                decision={"action": "blocked",
                          "reason": "clarification loop guard"},
            ))
            await s.commit()
            return

        # 3. Normal path: write the attempt row + transition + publish.
        parent = (
            await s.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        s.add(ArchitectAttempt(
            task_id=parent_task_id,
            phase=phase,
            cycle=prior + 1,
            reasoning=output,
            tool_calls=tool_calls,
            clarification_question=question,
            session_blob_path=session_blob_path,
            decision={"action": "awaiting_clarification"},
        ))
        await transition(
            s, parent, TaskStatus.AWAITING_CLARIFICATION,
            message="Architect needs answers",
        )
        await s.commit()

    await publish(Event(
        type=TaskEventType.ARCHITECT_CLARIFICATION_NEEDED,
        task_id=parent_task_id,
        payload={"question": question},
    ))
    log.info(
        "architect.clarification.emitted",
        task_id=parent_task_id, round=prior + 1, question_preview=question[:120],
    )


def _next_cycle_sync(n: int) -> int:
    """Small helper for tests / clarity. Cycles are 1-indexed."""
    return n
```

Also add at the top of `architect.py` (with other imports) — if not already imported:

```python
import os
from sqlalchemy import func
```

- [ ] **Step 5: Verify pass**

Run: `DATABASE_URL=postgresql+asyncpg://autoagent:changeme@localhost:5432/autoagent .venv/bin/python3 -m pytest tests/test_architect_emit_clarification.py -v`
Expected: PASS.

- [ ] **Step 6: Sanity-check no regression**

Run: `.venv/bin/python3 -m pytest tests/test_architect_extractors.py tests/test_architect_run_initial.py -v 2>&1 | tail -15`
Expected: all PASS (the existing run_initial tests still work because the clarification check returns None for backlog output).

- [ ] **Step 7: Commit**

```bash
git add agent/lifecycle/trio/architect.py tests/test_architect_emit_clarification.py
git commit -m "feat(trio): run_initial recognises awaiting_clarification + loop guard"
```

---

### Task 9: `checkpoint` and `run_revision` also recognise clarification

**Files:**
- Modify: `agent/lifecycle/trio/architect.py` (`checkpoint`, `run_revision`)
- Test: `tests/test_architect_emit_clarification.py` (extend)

- [ ] **Step 1: Extend the test**

Append to `tests/test_architect_emit_clarification.py`:

```python
@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_checkpoint_handles_awaiting_clarification(session, publisher):
    """Checkpoint pass can also emit awaiting_clarification."""
    from shared.models import (
        ArchitectAttempt, Organization, Repo, Task, TaskComplexity,
        TaskSource, TaskStatus, TrioPhase,
    )

    org = Organization(name="t2", slug="t2")
    session.add(org)
    await session.flush()
    repo = Repo(name="r2", url="https://github.com/o/r2.git",
                organization_id=org.id, default_branch="main")
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING, complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        trio_phase=TrioPhase.ARCHITECT_CHECKPOINT,
        trio_backlog=[{"id": "1", "title": "x", "description": "y",
                       "status": "done"}],
    )
    session.add(parent)
    # seed one INITIAL attempt with a commit so checkpoint has lineage
    session.add(ArchitectAttempt(
        task_id=parent.id, phase="INITIAL", cycle=1,
        reasoning="r", commit_sha="abc1234", tool_calls=[],
    ))
    await session.commit()

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=MagicMock(
        output=(
            'Reviewing the merge.\n```json\n'
            '{"decision": {"action": "awaiting_clarification", '
            '"question": "Should we add caching?"}}\n```'
        ),
        messages=[MagicMock(role="user", content="seed")],
        api_messages=[MagicMock(role="user", content="seed")],
    ))

    with patch("agent.lifecycle.trio.architect.create_architect_agent",
               return_value=fake_agent), \
         patch("agent.lifecycle.trio.architect._prepare_parent_workspace",
               AsyncMock(return_value=MagicMock(root="/tmp/ws"))), \
         patch("agent.lifecycle.trio.architect.home_dir_for_task",
               AsyncMock(return_value=None)), \
         patch("agent.session.Session.save", AsyncMock()):
        from agent.lifecycle.trio import architect
        await architect.checkpoint(parent.id, child_task_id=99)

    await session.refresh(parent)
    assert parent.status == TaskStatus.AWAITING_CLARIFICATION

    from sqlalchemy import select as _sel
    attempts = (await session.execute(
        _sel(ArchitectAttempt).where(ArchitectAttempt.task_id == parent.id)
        .order_by(ArchitectAttempt.id)
    )).scalars().all()
    # Two rows: the seeded INITIAL + a new CHECKPOINT with the question.
    assert len(attempts) == 2
    assert attempts[-1].clarification_question == "Should we add caching?"

    needed = [e for e in publisher.events
              if e.type == TaskEventType.ARCHITECT_CLARIFICATION_NEEDED]
    assert len(needed) == 1
```

- [ ] **Step 2: Verify failure**

Run: `DATABASE_URL=postgresql+asyncpg://autoagent:changeme@localhost:5432/autoagent .venv/bin/python3 -m pytest tests/test_architect_emit_clarification.py::test_checkpoint_handles_awaiting_clarification -v`
Expected: FAIL.

- [ ] **Step 3: Patch `checkpoint`**

In `agent/lifecycle/trio/architect.py`, find `async def checkpoint(...)`. After the `output = _result_output(run_result)` line, before the existing `_extract_*` calls, insert:

```python
    clarification = _extract_clarification(output)
    if clarification is not None:
        await _emit_clarification(
            parent_task_id=parent_task_id,
            agent=agent,
            workspace=workspace,
            output=output,
            tool_calls=tool_calls,
            question=clarification,
            phase=ArchitectPhase.CHECKPOINT,
        )
        return
```

- [ ] **Step 4: Patch `run_revision`**

Same insertion immediately after `output = _result_output(run_result)` in `run_revision`:

```python
    clarification = _extract_clarification(output)
    if clarification is not None:
        await _emit_clarification(
            parent_task_id=parent_task_id,
            agent=agent,
            workspace=workspace,
            output=output,
            tool_calls=tool_calls,
            question=clarification,
            phase=ArchitectPhase.REVISION,
        )
        return
```

- [ ] **Step 5: Verify pass**

Run: `DATABASE_URL=... .venv/bin/python3 -m pytest tests/test_architect_emit_clarification.py -v`
Expected: 2 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/lifecycle/trio/architect.py tests/test_architect_emit_clarification.py
git commit -m "feat(trio): checkpoint + run_revision also handle awaiting_clarification"
```

---

### Task 10: Loop guard test

**Files:**
- Test: `tests/test_trio_clarification_loop_guard.py` (new)

- [ ] **Step 1: Write the test**

Create `tests/test_trio_clarification_loop_guard.py`:

```python
"""Loop guard — after TRIO_MAX_CLARIFICATIONS rounds, parent goes BLOCKED."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_fourth_clarification_blocks_parent(session, publisher):
    """3 prior clarification rounds + a 4th → parent transitions to BLOCKED."""
    from shared.models import (
        ArchitectAttempt, Organization, Repo, Task, TaskComplexity,
        TaskSource, TaskStatus, TrioPhase,
    )

    org = Organization(name="lg", slug="lg")
    session.add(org)
    await session.flush()
    repo = Repo(name="r", url="https://github.com/o/r.git",
                organization_id=org.id, default_branch="main")
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING, complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.flush()
    # Seed three prior clarification rounds.
    for cycle in (1, 2, 3):
        session.add(ArchitectAttempt(
            task_id=parent.id, phase="INITIAL", cycle=cycle,
            reasoning=f"round {cycle}", tool_calls=[],
            clarification_question=f"Q{cycle}",
            clarification_answer=f"A{cycle}",
            clarification_source="po",
            session_blob_path=f"trio-{parent.id}.json",
        ))
    await session.commit()

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=MagicMock(
        output=(
            '```json\n{"decision": {"action": "awaiting_clarification", '
            '"question": "Q4"}}\n```'
        ),
        messages=[], api_messages=[],
    ))

    with patch("agent.lifecycle.trio.architect.create_architect_agent",
               return_value=fake_agent), \
         patch("agent.lifecycle.trio.architect._prepare_parent_workspace",
               AsyncMock(return_value=MagicMock(root="/tmp/ws"))), \
         patch("agent.lifecycle.trio.architect.home_dir_for_task",
               AsyncMock(return_value=None)), \
         patch("agent.session.Session.save", AsyncMock()), \
         patch.dict(os.environ, {"TRIO_MAX_CLARIFICATIONS": "3"}):
        from agent.lifecycle.trio import architect
        await architect.run_initial(parent.id)

    await session.refresh(parent)
    assert parent.status == TaskStatus.BLOCKED
```

- [ ] **Step 2: Verify pass**

Run: `DATABASE_URL=... .venv/bin/python3 -m pytest tests/test_trio_clarification_loop_guard.py -v`
Expected: PASS (the `_emit_clarification` helper from Task 8 already implements the guard).

- [ ] **Step 3: Commit**

```bash
git add tests/test_trio_clarification_loop_guard.py
git commit -m "test(trio): clarification loop guard transitions to BLOCKED on 4th round"
```

---

## Phase 4: Architect Resume

### Task 11: `architect.resume(parent_task_id)`

**Files:**
- Modify: `agent/lifecycle/trio/architect.py` (add `resume`)
- Test: `tests/test_architect_resume.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_architect_resume.py`:

```python
"""architect.resume loads session, injects answer, continues AgentLoop."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_resume_continues_agent_with_answer_as_user_message(
    session, publisher,
):
    from shared.models import (
        ArchitectAttempt, Organization, Repo, Task, TaskComplexity,
        TaskSource, TaskStatus, TrioPhase,
    )

    org = Organization(name="rs", slug="rs")
    session.add(org)
    await session.flush()
    repo = Repo(name="r", url="https://github.com/o/r.git",
                organization_id=org.id, default_branch="main")
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING, complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.flush()
    session.add(ArchitectAttempt(
        task_id=parent.id, phase="INITIAL", cycle=1,
        reasoning="prior reasoning", tool_calls=[],
        clarification_question="Pick React or Vue?",
        clarification_answer="React, the team knows it.",
        clarification_source="po",
        session_blob_path=f"trio-{parent.id}.json",
    ))
    await session.commit()

    fake_agent = MagicMock()
    # Simulate the architect emitting a backlog on resume.
    fake_agent.run = AsyncMock(return_value=MagicMock(
        output=(
            'Got it, going with React.\n```json\n'
            '{"backlog": [{"id": "1", "title": "Setup React app", '
            '"description": "Scaffold create-react-app"}]}\n```'
        ),
        messages=[], api_messages=[],
    ))
    fake_agent.messages = []
    fake_agent.api_messages = []

    with patch("agent.lifecycle.trio.architect.create_architect_agent",
               return_value=fake_agent) as create_mock, \
         patch("agent.lifecycle.trio.architect._prepare_parent_workspace",
               AsyncMock(return_value=MagicMock(root="/tmp/ws"))), \
         patch("agent.lifecycle.trio.architect.home_dir_for_task",
               AsyncMock(return_value=None)), \
         patch("agent.session.Session.load",
               AsyncMock(return_value=([], []))), \
         patch("agent.lifecycle.trio.architect._commit_and_open_initial_pr",
               AsyncMock(return_value="deadbeef")):
        from agent.lifecycle.trio import architect
        await architect.resume(parent.id)

    # The resume prompt should reference the answer.
    call_args = fake_agent.run.call_args
    prompt = call_args.args[0] if call_args.args else call_args.kwargs.get("prompt", "")
    assert "ANSWER FROM PRODUCT OWNER" in prompt
    assert "React, the team knows it." in prompt

    # Parent should now have a backlog populated.
    await session.refresh(parent)
    assert parent.trio_backlog is not None
    assert len(parent.trio_backlog) == 1


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_resume_falls_back_when_session_blob_missing(session, publisher):
    """If Session.load returns None, resume calls run_initial with the Q&A
    appended to the task description as additional context."""
    from shared.models import (
        ArchitectAttempt, Organization, Repo, Task, TaskComplexity,
        TaskSource, TaskStatus, TrioPhase,
    )

    org = Organization(name="rl", slug="rl")
    session.add(org)
    await session.flush()
    repo = Repo(name="r", url="https://github.com/o/r.git",
                organization_id=org.id, default_branch="main")
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="original description",
        source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING, complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.flush()
    session.add(ArchitectAttempt(
        task_id=parent.id, phase="INITIAL", cycle=1,
        reasoning="prior", tool_calls=[],
        clarification_question="Q?",
        clarification_answer="A.",
        clarification_source="user",
        session_blob_path=f"trio-{parent.id}.json",
    ))
    await session.commit()

    run_initial_mock = AsyncMock()
    with patch("agent.session.Session.load", AsyncMock(return_value=None)), \
         patch("agent.lifecycle.trio.architect.run_initial", run_initial_mock):
        from agent.lifecycle.trio import architect
        await architect.resume(parent.id)

    run_initial_mock.assert_called_once_with(parent.id)
```

- [ ] **Step 2: Verify failure**

Run: `DATABASE_URL=... .venv/bin/python3 -m pytest tests/test_architect_resume.py -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'resume'`.

- [ ] **Step 3: Implement `architect.resume`**

In `agent/lifecycle/trio/architect.py`, add at the bottom of the file (after `run_revision`):

```python
async def resume(parent_task_id: int) -> None:
    """Resume the architect after a clarification answer has been written.

    Reloads the AgentLoop session from the workspace, injects the answer
    as a synthetic user message, runs the AgentLoop to completion, then
    handles the output the same way run_initial / checkpoint do — either
    a backlog (continue normally) or another clarification (loop guard
    applies).

    Falls back to ``run_initial`` if the session blob can't be loaded
    (workspace wiped or first save failed).
    """
    from agent.lifecycle.factory import home_dir_for_task
    from agent.session import Session

    async with async_session() as s:
        parent = (
            await s.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one()
        attempt = (
            await s.execute(
                select(ArchitectAttempt)
                .where(ArchitectAttempt.task_id == parent_task_id)
                .where(ArchitectAttempt.clarification_answer.is_not(None))
                .order_by(ArchitectAttempt.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if attempt is None:
            log.warning(
                "architect.resume.no_answered_attempt",
                task_id=parent_task_id,
            )
            return
        task_description = parent.description or parent.title
        task_title = parent.title
        repo_name = parent.repo.name if parent.repo else None
        org_id = parent.organization_id
        home_dir = await home_dir_for_task(parent)
        answer = attempt.clarification_answer
        source = (attempt.clarification_source or "user").upper()
        phase_for_resume = attempt.phase  # INITIAL | CHECKPOINT | REVISION

    workspace = await _prepare_parent_workspace(parent)
    session_id = f"trio-{parent_task_id}"
    storage_dir = workspace.root if hasattr(workspace, "root") else str(workspace)
    file_session = Session(session_id=session_id, storage_dir=storage_dir)
    loaded = await file_session.load()
    if loaded is None:
        log.warning(
            "architect.resume.session_lost",
            task_id=parent_task_id, path=storage_dir,
        )
        # Fallback: re-run run_initial. The Q&A already exists on the
        # architect_attempts row and is visible to the architect via
        # context_collapse's recent-history injection (and to humans in
        # the UI), so we don't need to thread it into the prompt.
        await run_initial(parent_task_id)
        return

    # Reconstruct an AgentLoop, attach the session for resume.
    agent = create_architect_agent(
        workspace=workspace,
        task_id=parent_task_id,
        task_description=task_description,
        phase="initial" if phase_for_resume == ArchitectPhase.INITIAL
              else "checkpoint" if phase_for_resume == ArchitectPhase.CHECKPOINT
              else "revision",
        repo_name=repo_name,
        home_dir=home_dir,
        org_id=org_id,
        session=file_session,  # AgentLoop will auto-load prior messages
    )

    prompt = (
        f"ANSWER FROM {source}:\n\n{answer}\n\n"
        f"Now produce the JSON for this phase "
        f"(backlog for INITIAL/REVISION, decision for CHECKPOINT)."
    )
    run_result = await agent.run(prompt, resume=True)
    output = _result_output(run_result)
    tool_calls = _result_tool_calls(run_result, agent)

    # Funnel into the same parsing logic as a fresh run.
    clarification = _extract_clarification(output)
    if clarification is not None:
        # Another round — loop guard applies inside _emit_clarification.
        await _emit_clarification(
            parent_task_id=parent_task_id,
            agent=agent, workspace=workspace,
            output=output, tool_calls=tool_calls,
            question=clarification, phase=phase_for_resume,
        )
        return

    if phase_for_resume in (ArchitectPhase.INITIAL, ArchitectPhase.REVISION):
        backlog = _extract_backlog(output)
        if backlog is None:
            await _emit_blocked(
                parent_task_id, output, tool_calls,
                reason="invalid JSON on resume",
                phase=phase_for_resume,
            )
            return
        commit_sha = await _commit_and_open_initial_pr(parent, workspace)
        async with async_session() as s:
            parent = (
                await s.execute(select(Task).where(Task.id == parent_task_id))
            ).scalar_one()
            parent.trio_backlog = backlog
            s.add(ArchitectAttempt(
                task_id=parent_task_id, phase=phase_for_resume,
                cycle=attempt.cycle + 1,
                reasoning=output, commit_sha=commit_sha or None,
                tool_calls=tool_calls,
            ))
            await s.commit()
    else:  # CHECKPOINT
        # Reuse the checkpoint output handling — extract backlog + decision.
        await _persist_checkpoint_attempt(
            parent_task_id, output, tool_calls,
            cycle=attempt.cycle + 1,
        )
```

> **Note:** `_emit_blocked` and `_persist_checkpoint_attempt` are existing helpers in `architect.py`. If they have slightly different names in the live file, adapt the calls; the intent is "same path the existing flow uses after `_extract_backlog`/`_extract_checkpoint_decision`."

Also add the `session=file_session` kwarg in `create_architect_agent` — see Task 12 if it doesn't accept one yet.

- [ ] **Step 4: Verify pass**

Run: `DATABASE_URL=... .venv/bin/python3 -m pytest tests/test_architect_resume.py -v`
Expected: 2 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/lifecycle/trio/architect.py tests/test_architect_resume.py
git commit -m "feat(trio): architect.resume — reload session, inject answer, continue"
```

---

### Task 12: `create_architect_agent` accepts an optional `Session`

**Files:**
- Modify: `agent/lifecycle/trio/architect.py` (`create_architect_agent`)
- Test: covered by Task 11.

- [ ] **Step 1: Update the factory signature**

In `agent/lifecycle/trio/architect.py`, locate `def create_architect_agent(...)`. Add `session: "Session | None" = None` to the signature, and pass it through to the `AgentLoop` construction:

```python
def create_architect_agent(
    workspace,
    task_id: int,
    task_description: str,
    phase: str,
    *,
    repo_name: str | None = None,
    home_dir: str | None = None,
    org_id: int | None = None,
    session: "Session | None" = None,  # NEW — enables AgentLoop session resume
) -> AgentLoop:
    """..."""
    # ... existing setup ...
    loop = AgentLoop(
        # ... existing kwargs ...
        session=session,
    )
    # ... existing prompt-override setup ...
    return loop
```

Adapt the exact change to whatever the live signature looks like. The key is: a new `session` kwarg gets threaded into the `AgentLoop(...)` call.

- [ ] **Step 2: Verify the factory still builds without a session arg**

Run: `.venv/bin/python3 -m pytest tests/test_architect_factory.py -v 2>&1 | tail -10`
Expected: existing factory tests still PASS (default arg is None).

- [ ] **Step 3: Commit**

```bash
git add agent/lifecycle/trio/architect.py
git commit -m "feat(trio): create_architect_agent accepts optional Session for resume"
```

---

## Phase 5: PO Agent

### Task 13: `agent/po_agent.py::answer_architect_question`

**Files:**
- Create: `agent/po_agent.py`
- Test: `tests/test_po_agent_answer.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_po_agent_answer.py`:

```python
"""po_agent.answer_architect_question — writes answer + publishes RESOLVED."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.events import TaskEventType


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_answer_writes_answer_and_publishes_resolved(session, publisher):
    from shared.models import (
        ArchitectAttempt, Organization, Repo, Task, TaskComplexity,
        TaskSource, TaskStatus, TrioPhase,
    )

    org = Organization(name="po", slug="po")
    session.add(org)
    await session.flush()
    repo = Repo(
        name="r", url="https://github.com/o/r.git",
        organization_id=org.id, default_branch="main",
        product_brief="# Mission\nBuild a TODO app for parents.",
    )
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_CLARIFICATION,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        freeform_mode=True, trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.flush()
    session.add(ArchitectAttempt(
        task_id=parent.id, phase="INITIAL", cycle=1,
        reasoning="r", tool_calls=[],
        clarification_question="Should we support shared family lists?",
        session_blob_path=f"trio-{parent.id}.json",
    ))
    await session.commit()

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=MagicMock(
        output='```json\n{"answer": "Yes — shared lists are the headline feature."}\n```'
    ))

    with patch("agent.po_agent.create_agent", return_value=fake_agent), \
         patch("agent.po_agent.clone_repo",
               AsyncMock(return_value=MagicMock(root="/tmp/po-ws"))):
        from agent.po_agent import answer_architect_question
        await answer_architect_question(parent.id)

    # Answer written to the attempt row.
    from sqlalchemy import select as _sel
    attempt = (await session.execute(
        _sel(ArchitectAttempt).where(ArchitectAttempt.task_id == parent.id)
    )).scalar_one()
    assert "shared lists" in (attempt.clarification_answer or "")
    assert attempt.clarification_source == "po"

    # PO's prompt must contain product_brief.
    prompt = fake_agent.run.call_args.args[0]
    assert "Build a TODO app for parents." in prompt

    # *_RESOLVED published.
    resolved = [e for e in publisher.events
                if e.type == TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED]
    assert len(resolved) == 1
    assert resolved[0].task_id == parent.id


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_answer_handles_malformed_json(session, publisher):
    """When PO returns un-parseable JSON, the error is stored as the answer."""
    from shared.models import (
        ArchitectAttempt, Organization, Repo, Task, TaskComplexity,
        TaskSource, TaskStatus, TrioPhase,
    )

    org = Organization(name="poe", slug="poe")
    session.add(org)
    await session.flush()
    repo = Repo(name="r", url="https://github.com/o/r.git",
                organization_id=org.id, default_branch="main")
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_CLARIFICATION,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        freeform_mode=True, trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.flush()
    session.add(ArchitectAttempt(
        task_id=parent.id, phase="INITIAL", cycle=1,
        reasoning="r", tool_calls=[],
        clarification_question="Q?",
        session_blob_path=f"trio-{parent.id}.json",
    ))
    await session.commit()

    fake_agent = MagicMock()
    fake_agent.run = AsyncMock(return_value=MagicMock(
        output="No JSON here, just prose."
    ))

    with patch("agent.po_agent.create_agent", return_value=fake_agent), \
         patch("agent.po_agent.clone_repo",
               AsyncMock(return_value=MagicMock(root="/tmp/po-ws"))):
        from agent.po_agent import answer_architect_question
        await answer_architect_question(parent.id)

    from sqlalchemy import select as _sel
    attempt = (await session.execute(
        _sel(ArchitectAttempt).where(ArchitectAttempt.task_id == parent.id)
    )).scalar_one()
    assert attempt.clarification_answer is not None
    assert "could not parse" in attempt.clarification_answer.lower() or \
           "no parseable" in attempt.clarification_answer.lower()
    assert attempt.clarification_source == "po"
```

- [ ] **Step 2: Verify failure**

Run: `DATABASE_URL=... .venv/bin/python3 -m pytest tests/test_po_agent_answer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.po_agent'`.

- [ ] **Step 3: Create `agent/po_agent.py`**

```python
"""Product Owner agent — answers architect clarification questions.

Distinct from agent/po_analyzer.py which generates suggestions on a cron.
This module is a focused entry point: read the architect's question for
a trio parent, build a prompt that injects Repo.product_brief + the
current ARCHITECTURE.md, run a readonly agent, parse {"answer": "..."},
write it to the architect_attempts row, and publish
ARCHITECT_CLARIFICATION_RESOLVED.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select

from agent.lifecycle.factory import create_agent
from agent.workspace import clone_repo
from shared.database import async_session
from shared.events import Event, TaskEventType, publish
from shared.llm import parse_json_response  # see note below
from shared.models import ArchitectAttempt, Repo, Task

log = logging.getLogger(__name__)

# parse_json_response actually lives in agent/llm/structured.py — if the
# import above fails for your tree, swap to:
#   from agent.llm.structured import parse_json_response


async def answer_architect_question(parent_task_id: int) -> None:
    """Run the PO to answer the architect's outstanding clarification.

    Reads the latest architect_attempts row for parent_task_id where
    clarification_question IS NOT NULL AND clarification_answer IS NULL.
    Loads Repo.product_brief. Builds a readonly agent. Writes the answer
    (or a failure note) to the row and publishes
    ARCHITECT_CLARIFICATION_RESOLVED so the dispatcher can resume the
    architect.
    """
    async with async_session() as s:
        parent = (
            await s.execute(select(Task).where(Task.id == parent_task_id))
        ).scalar_one_or_none()
        if parent is None:
            log.warning("po_agent.parent_missing", task_id=parent_task_id)
            return
        attempt = (
            await s.execute(
                select(ArchitectAttempt)
                .where(ArchitectAttempt.task_id == parent_task_id)
                .where(ArchitectAttempt.clarification_question.is_not(None))
                .where(ArchitectAttempt.clarification_answer.is_(None))
                .order_by(ArchitectAttempt.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if attempt is None:
            log.warning(
                "po_agent.no_pending_clarification", task_id=parent_task_id,
            )
            return
        question = attempt.clarification_question
        attempt_id = attempt.id
        repo = (
            await s.execute(select(Repo).where(Repo.id == parent.repo_id))
        ).scalar_one_or_none()
        if repo is None:
            log.warning("po_agent.repo_missing", task_id=parent_task_id)
            return
        product_brief = repo.product_brief or ""
        repo_url = repo.url
        repo_name = repo.name
        default_branch = repo.default_branch or "main"

    if not product_brief:
        log.warning(
            "po_agent.no_product_brief",
            repo=repo_name, task_id=parent_task_id,
        )

    # Clone readonly for code-grounded answers.
    workspace = await clone_repo(
        repo_url, parent_task_id, default_branch,
        workspace_name=f"po-{repo_name.replace('/', '-')}-{parent_task_id}",
    )

    arch_md = ""
    arch_path = Path(workspace.root) / "ARCHITECTURE.md"
    if arch_path.exists():
        try:
            arch_md = arch_path.read_text(errors="replace")[:4000]
        except OSError:
            arch_md = ""

    prompt_parts: list[str] = []
    if product_brief:
        prompt_parts.append(f"# Product Brief\n\n{product_brief}\n")
    if arch_md:
        prompt_parts.append(f"# Current ARCHITECTURE.md (excerpt)\n\n{arch_md}\n")
    prompt_parts.append(
        "You are the Product Owner. The architect has paused and asked\n"
        "the following question. Answer as the PO, grounded in the\n"
        "product brief above. Be specific and brief (max ~300 words).\n\n"
        f"Question:\n{question}\n\n"
        'Output ONLY a JSON object on its own lines:\n'
        '```json\n{"answer": "<your answer>"}\n```\n'
    )
    prompt = "\n\n".join(prompt_parts)

    agent = create_agent(
        workspace, readonly=True, max_turns=8,
        task_description=f"PO answers architect for task #{parent_task_id}",
        repo_name=repo_name,
    )

    try:
        result = await agent.run(prompt)
        output = getattr(result, "output", "") or ""
    except Exception as e:
        log.exception("po_agent.run_failed", task_id=parent_task_id)
        await _write_answer(
            attempt_id,
            f"(PO failed with an exception: {type(e).__name__}: {e!s})",
        )
        await publish(Event(
            type=TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED,
            task_id=parent_task_id,
        ))
        return

    parsed = parse_json_response(output)
    if not isinstance(parsed, dict) or "answer" not in parsed:
        log.warning(
            "po_agent.unparseable_output",
            task_id=parent_task_id, output_preview=output[:300],
        )
        answer = (
            "(PO returned no parseable answer. Raw output preview: "
            f"{output[:400]!r})"
        )
    else:
        answer = str(parsed["answer"])

    await _write_answer(attempt_id, answer)
    await publish(Event(
        type=TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED,
        task_id=parent_task_id,
    ))
    log.info("po_agent.answered", task_id=parent_task_id,
             answer_preview=answer[:120])


async def _write_answer(attempt_id: int, answer: str) -> None:
    async with async_session() as s:
        row = (
            await s.execute(
                select(ArchitectAttempt).where(ArchitectAttempt.id == attempt_id)
            )
        ).scalar_one()
        row.clarification_answer = answer
        row.clarification_source = "po"
        await s.commit()
```

- [ ] **Step 4: Verify import path**

If `from shared.llm import parse_json_response` fails:

```bash
grep -rn "def parse_json_response" agent/ shared/ 2>&1 | head -3
```

Adjust the import to the actual module (typically `agent.llm.structured`).

- [ ] **Step 5: Verify pass**

Run: `DATABASE_URL=... .venv/bin/python3 -m pytest tests/test_po_agent_answer.py -v`
Expected: 2 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/po_agent.py tests/test_po_agent_answer.py
git commit -m "feat(po): po_agent.answer_architect_question — readonly PO answers"
```

---

## Phase 6: Dispatcher + Inbound Seam

### Task 14: Dispatcher handlers in `run.py`

**Files:**
- Modify: `run.py`
- Test: `tests/test_clarification_dispatcher.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_clarification_dispatcher.py`:

```python
"""Dispatcher: ARCHITECT_CLARIFICATION_NEEDED routes by freeform_mode."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.events import Event, TaskEventType


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_freeform_dispatches_po_agent(session, publisher):
    from shared.models import (
        Organization, Repo, Task, TaskComplexity,
        TaskSource, TaskStatus, TrioPhase,
    )

    org = Organization(name="d", slug="d")
    session.add(org)
    await session.flush()
    repo = Repo(name="r", url="https://github.com/o/r.git",
                organization_id=org.id, default_branch="main")
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_CLARIFICATION,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        freeform_mode=True, trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.commit()

    po_mock = AsyncMock()
    with patch("agent.po_agent.answer_architect_question", po_mock):
        from run import on_architect_clarification_needed
        await on_architect_clarification_needed(Event(
            type=TaskEventType.ARCHITECT_CLARIFICATION_NEEDED,
            task_id=parent.id,
            payload={"question": "Q?"},
        ))
        # Give the create_task a tick to run.
        import asyncio
        await asyncio.sleep(0)

    po_mock.assert_called_once_with(parent.id)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_non_freeform_republishes_clarification_needed(session, publisher):
    from shared.models import (
        Organization, Repo, Task, TaskComplexity,
        TaskSource, TaskStatus, TrioPhase,
    )

    org = Organization(name="dn", slug="dn")
    session.add(org)
    await session.flush()
    repo = Repo(name="r", url="https://github.com/o/r.git",
                organization_id=org.id, default_branch="main")
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_CLARIFICATION,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        freeform_mode=False, trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.commit()

    from run import on_architect_clarification_needed
    await on_architect_clarification_needed(Event(
        type=TaskEventType.ARCHITECT_CLARIFICATION_NEEDED,
        task_id=parent.id,
        payload={"question": "Q?"},
    ))

    surfaced = [e for e in publisher.events
                if e.type == TaskEventType.CLARIFICATION_NEEDED
                and e.task_id == parent.id]
    assert len(surfaced) == 1
    assert surfaced[0].payload["question"] == "Q?"
    assert surfaced[0].payload["phase"] == "trio_architect"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_resolved_transitions_and_resumes(session, publisher):
    from shared.models import (
        ArchitectAttempt, Organization, Repo, Task, TaskComplexity,
        TaskSource, TaskStatus, TrioPhase,
    )

    org = Organization(name="dr", slug="dr")
    session.add(org)
    await session.flush()
    repo = Repo(name="r", url="https://github.com/o/r.git",
                organization_id=org.id, default_branch="main")
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_CLARIFICATION,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        freeform_mode=True, trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.flush()
    session.add(ArchitectAttempt(
        task_id=parent.id, phase="INITIAL", cycle=1,
        reasoning="r", tool_calls=[],
        clarification_question="Q?", clarification_answer="A.",
        clarification_source="po",
        session_blob_path=f"trio-{parent.id}.json",
    ))
    await session.commit()

    resume_mock = AsyncMock()
    with patch("agent.lifecycle.trio.architect.resume", resume_mock):
        from run import on_architect_clarification_resolved
        await on_architect_clarification_resolved(Event(
            type=TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED,
            task_id=parent.id,
        ))
        import asyncio
        await asyncio.sleep(0)

    await session.refresh(parent)
    assert parent.status == TaskStatus.TRIO_EXECUTING
    resume_mock.assert_called_once_with(parent.id)
```

- [ ] **Step 2: Verify failure**

Run: `DATABASE_URL=... .venv/bin/python3 -m pytest tests/test_clarification_dispatcher.py -v`
Expected: FAIL with `ImportError: cannot import name 'on_architect_clarification_needed'`.

- [ ] **Step 3: Add the two handlers in run.py**

In `run.py`, after the existing `on_clarification_resolved` handler (search for it), add:

```python
async def on_architect_clarification_needed(event: Event) -> None:
    """Route the architect's clarification question.

    Freeform → dispatch the PO agent (it writes the answer and publishes
    *_RESOLVED). Non-freeform → republish the planner-style
    CLARIFICATION_NEEDED event with phase='trio_architect' so the
    existing per-integration formatters in integrations/* surface the
    question via the right channel.
    """
    async with async_session() as session:
        task = await get_task(session, event.task_id)
        if not task or task.status != TaskStatus.AWAITING_CLARIFICATION:
            return
        if task.trio_phase is None:
            # Not a trio clarification. Ignore.
            return
        if task.freeform_mode:
            import agent.po_agent as po_agent
            asyncio.create_task(  # noqa: RUF006 fire-and-forget
                po_agent.answer_architect_question(task.id)
            )
        else:
            await publish(Event(
                type=TaskEventType.CLARIFICATION_NEEDED,
                task_id=task.id,
                payload={
                    "question": event.payload.get("question", ""),
                    "phase": "trio_architect",
                },
            ))


async def on_architect_clarification_resolved(event: Event) -> None:
    """Answer landed; transition state and dispatch architect.resume."""
    async with async_session() as session:
        task = await get_task(session, event.task_id)
        if not task or task.status != TaskStatus.AWAITING_CLARIFICATION:
            return
        if task.trio_phase is None:
            return
        await transition(
            session, task, TaskStatus.TRIO_EXECUTING,
            "Architect resuming after clarification",
        )
        await session.commit()
        from agent.lifecycle.trio import architect
        asyncio.create_task(  # noqa: RUF006
            architect.resume(task.id)
        )
```

Then in the `bus.on(...)` wiring block at the bottom of `run.py` (search for `bus.on(TaskEventType.CLARIFIED`), add:

```python
bus.on(TaskEventType.ARCHITECT_CLARIFICATION_NEEDED, on_architect_clarification_needed)
bus.on(TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED, on_architect_clarification_resolved)
```

- [ ] **Step 4: Verify pass**

Run: `DATABASE_URL=... .venv/bin/python3 -m pytest tests/test_clarification_dispatcher.py -v`
Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add run.py tests/test_clarification_dispatcher.py
git commit -m "feat(trio): dispatcher for ARCHITECT_CLARIFICATION_NEEDED + RESOLVED"
```

---

### Task 15: Inbound message seam — route by `trio_phase`

**Files:**
- Modify: the existing inbound dispatch point — typically `handle_clarification_response` is called from web/Slack/Telegram adapters. We add a new wrapper `handle_clarification_inbound` that they call instead. (Check `agent/lifecycle/conversation.py` for current entry points.)
- Modify: callers — `integrations/*` and `orchestrator/router.py` (the POST `/tasks/{id}/messages` endpoint).
- Test: `tests/test_clarification_inbound.py` (new)

- [ ] **Step 1: Find the current callers**

Run: `grep -rn "handle_clarification_response" /Users/alanyeginchibayev/Documents/Github/auto-agent --include="*.py" | grep -v test_ | head -10`

Note each call site — these are where you'll insert the trio fork.

- [ ] **Step 2: Write the failing test**

Create `tests/test_clarification_inbound.py`:

```python
"""handle_clarification_inbound dispatches by trio_phase."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from shared.events import TaskEventType


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_inbound_with_trio_phase_writes_answer_and_publishes(
    session, publisher,
):
    from shared.models import (
        ArchitectAttempt, Organization, Repo, Task, TaskComplexity,
        TaskSource, TaskStatus, TrioPhase,
    )

    org = Organization(name="ib", slug="ib")
    session.add(org)
    await session.flush()
    repo = Repo(name="r", url="https://github.com/o/r.git",
                organization_id=org.id, default_branch="main")
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_CLARIFICATION,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        freeform_mode=False, trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.flush()
    session.add(ArchitectAttempt(
        task_id=parent.id, phase="INITIAL", cycle=1,
        reasoning="r", tool_calls=[],
        clarification_question="Q?",
        session_blob_path=f"trio-{parent.id}.json",
    ))
    await session.commit()

    from agent.lifecycle.conversation import handle_clarification_inbound
    await handle_clarification_inbound(parent.id, "Go with React, simpler.")

    from sqlalchemy import select as _sel
    attempt = (await session.execute(
        _sel(ArchitectAttempt).where(ArchitectAttempt.task_id == parent.id)
    )).scalar_one()
    assert attempt.clarification_answer == "Go with React, simpler."
    assert attempt.clarification_source == "user"

    resolved = [e for e in publisher.events
                if e.type == TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED
                and e.task_id == parent.id]
    assert len(resolved) == 1


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_inbound_without_trio_phase_delegates_to_existing_handler(
    session,
):
    """Planner case: trio_phase IS NULL → delegate to handle_clarification_response."""
    from shared.models import (
        Organization, Repo, Task, TaskComplexity,
        TaskSource, TaskStatus,
    )

    org = Organization(name="ibn", slug="ibn")
    session.add(org)
    await session.flush()
    repo = Repo(name="r", url="https://github.com/o/r.git",
                organization_id=org.id, default_branch="main")
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_CLARIFICATION,
        complexity=TaskComplexity.COMPLEX,
        repo_id=repo.id, organization_id=org.id,
    )
    session.add(parent)
    await session.commit()

    delegate = AsyncMock()
    with patch("agent.lifecycle.conversation.handle_clarification_response",
               delegate):
        from agent.lifecycle.conversation import handle_clarification_inbound
        await handle_clarification_inbound(parent.id, "Use Postgres.")

    delegate.assert_called_once_with(parent.id, "Use Postgres.")
```

- [ ] **Step 3: Verify failure**

Run: `DATABASE_URL=... .venv/bin/python3 -m pytest tests/test_clarification_inbound.py -v`
Expected: FAIL with `ImportError: cannot import name 'handle_clarification_inbound'`.

- [ ] **Step 4: Add `handle_clarification_inbound`**

In `agent/lifecycle/conversation.py`, append:

```python
async def handle_clarification_inbound(task_id: int, content: str) -> None:
    """Single entry point for an inbound clarification answer.

    Dispatches by `task.trio_phase`:
    - Set → trio architect is waiting. Write the answer to the pending
      ArchitectAttempt row and publish ARCHITECT_CLARIFICATION_RESOLVED.
    - None → existing planner clarification. Delegate to
      handle_clarification_response (Claude Code CLI session resume).

    Idempotent on the trio side: once clarification_answer is set, the
    second message is dropped (the existing inbound channels should also
    log it as a regular task_message before calling here, so the user's
    follow-up is still visible).
    """
    from sqlalchemy import select as _sel
    from shared.database import async_session as _sess
    from shared.events import Event, TaskEventType, publish
    from shared.models import ArchitectAttempt, Task, TaskStatus

    async with _sess() as s:
        task = (
            await s.execute(_sel(Task).where(Task.id == task_id))
        ).scalar_one_or_none()
        if task is None:
            return
        if task.status != TaskStatus.AWAITING_CLARIFICATION:
            return
        if task.trio_phase is None:
            await handle_clarification_response(task_id, content)
            return
        attempt = (
            await s.execute(
                _sel(ArchitectAttempt)
                .where(ArchitectAttempt.task_id == task_id)
                .where(ArchitectAttempt.clarification_question.is_not(None))
                .where(ArchitectAttempt.clarification_answer.is_(None))
                .order_by(ArchitectAttempt.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if attempt is None:
            log.warning(
                "trio.inbound.no_pending_attempt", task_id=task_id,
            )
            return
        attempt.clarification_answer = content
        attempt.clarification_source = "user"
        await s.commit()

    await publish(Event(
        type=TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED,
        task_id=task_id,
    ))
```

- [ ] **Step 5: Update callers**

For each caller of `handle_clarification_response` that comes from an inbound user message (typically the web `POST /api/tasks/{id}/messages` endpoint in `orchestrator/router.py`, plus Slack/Telegram adapters in `integrations/*`), change the call to `handle_clarification_inbound`. For each call site:

```bash
grep -rn "handle_clarification_response" --include="*.py" /Users/alanyeginchibayev/Documents/Github/auto-agent 2>&1 | grep -v test_ | grep -v conversation.py
```

Replace `handle_clarification_response(task_id, content)` with `handle_clarification_inbound(task_id, content)` at each non-test, non-conversation.py site. `handle_clarification_response` stays as a private leaf — only `handle_clarification_inbound` calls it now (for the planner path).

- [ ] **Step 6: Verify pass**

Run: `DATABASE_URL=... .venv/bin/python3 -m pytest tests/test_clarification_inbound.py -v`
Expected: 2 tests PASS.

- [ ] **Step 7: Sanity-check no regression in planner clarification path**

Run: `.venv/bin/python3 -m pytest tests/test_conversation.py tests/test_clarification.py 2>&1 | tail -10` (run whichever planner-clarification tests exist).
Expected: existing tests PASS (or skip if DB-bound).

- [ ] **Step 8: Commit**

```bash
git add agent/lifecycle/conversation.py orchestrator/router.py integrations/ tests/test_clarification_inbound.py
git commit -m "feat(trio): handle_clarification_inbound — route by trio_phase"
```

---

## Phase 7: Recovery

### Task 16: Crash recovery for `AWAITING_CLARIFICATION` + `trio_phase` set

**Files:**
- Modify: `agent/lifecycle/trio/recovery.py`
- Test: `tests/test_trio_recovery_clarification.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_trio_recovery_clarification.py`:

```python
"""Recovery on restart: AWAITING_CLARIFICATION + trio_phase set + answer
written pre-crash → re-publish RESOLVED so the architect resumes."""
from __future__ import annotations

import os

import pytest

from shared.events import TaskEventType


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_recovery_republishes_resolved_when_answer_landed_pre_crash(
    session, publisher,
):
    from shared.models import (
        ArchitectAttempt, Organization, Repo, Task, TaskComplexity,
        TaskSource, TaskStatus, TrioPhase,
    )

    org = Organization(name="rc", slug="rc")
    session.add(org)
    await session.flush()
    repo = Repo(name="r", url="https://github.com/o/r.git",
                organization_id=org.id, default_branch="main")
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_CLARIFICATION,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        freeform_mode=True, trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.flush()
    # Pre-crash: answer was written but resume hadn't fired yet.
    session.add(ArchitectAttempt(
        task_id=parent.id, phase="INITIAL", cycle=1,
        reasoning="r", tool_calls=[],
        clarification_question="Q?", clarification_answer="A.",
        clarification_source="po",
        session_blob_path=f"trio-{parent.id}.json",
    ))
    await session.commit()

    from agent.lifecycle.trio.recovery import resume_all_trio_parents
    await resume_all_trio_parents()

    resolved = [e for e in publisher.events
                if e.type == TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED
                and e.task_id == parent.id]
    assert len(resolved) == 1


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_recovery_does_not_publish_when_still_waiting(session, publisher):
    """No answer yet → recovery does nothing (still waiting on a human)."""
    from shared.models import (
        ArchitectAttempt, Organization, Repo, Task, TaskComplexity,
        TaskSource, TaskStatus, TrioPhase,
    )

    org = Organization(name="rcw", slug="rcw")
    session.add(org)
    await session.flush()
    repo = Repo(name="r", url="https://github.com/o/r.git",
                organization_id=org.id, default_branch="main")
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.AWAITING_CLARIFICATION,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        freeform_mode=False, trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.flush()
    session.add(ArchitectAttempt(
        task_id=parent.id, phase="INITIAL", cycle=1,
        reasoning="r", tool_calls=[],
        clarification_question="Q?",
        # No answer yet.
        session_blob_path=f"trio-{parent.id}.json",
    ))
    await session.commit()

    from agent.lifecycle.trio.recovery import resume_all_trio_parents
    await resume_all_trio_parents()

    resolved = [e for e in publisher.events
                if e.type == TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED
                and e.task_id == parent.id]
    assert resolved == []
```

- [ ] **Step 2: Verify failure**

Run: `DATABASE_URL=... .venv/bin/python3 -m pytest tests/test_trio_recovery_clarification.py -v`
Expected: FAIL (current `resume_all_trio_parents` only handles TRIO_EXECUTING).

- [ ] **Step 3: Extend `resume_all_trio_parents`**

In `agent/lifecycle/trio/recovery.py`, at the bottom of the function (after the existing TRIO_EXECUTING loop), add:

```python
    # NEW: AWAITING_CLARIFICATION + trio_phase set. The architect is
    # paused; if the answer landed pre-crash we re-publish RESOLVED so
    # on_architect_clarification_resolved transitions state and calls
    # architect.resume. If no answer yet, do nothing — we're still
    # waiting on a human / PO.
    async with async_session() as s:
        awaiting = (
            await s.execute(
                select(Task)
                .where(Task.status == TaskStatus.AWAITING_CLARIFICATION)
                .where(Task.trio_phase.is_not(None))
            )
        ).scalars().all()
        for task in awaiting:
            latest = (
                await s.execute(
                    select(ArchitectAttempt)
                    .where(ArchitectAttempt.task_id == task.id)
                    .where(ArchitectAttempt.clarification_question.is_not(None))
                    .order_by(ArchitectAttempt.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if latest is None:
                continue
            if latest.clarification_answer is None:
                log.info(
                    "trio.recovery.awaiting_clarification.still_waiting",
                    task_id=task.id,
                )
                continue
            log.info(
                "trio.recovery.awaiting_clarification.republish_resolved",
                task_id=task.id,
            )
            await publish(Event(
                type=TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED,
                task_id=task.id,
            ))
```

Also ensure imports at top of file include `ArchitectAttempt`, `TaskEventType`, `publish`, `Event` if not already.

- [ ] **Step 4: Verify pass**

Run: `DATABASE_URL=... .venv/bin/python3 -m pytest tests/test_trio_recovery_clarification.py -v`
Expected: 2 tests PASS.

- [ ] **Step 5: Sanity-check no regression**

Run: `.venv/bin/python3 -m pytest tests/test_trio_recovery.py 2>&1 | tail -10`
Expected: existing recovery tests PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/lifecycle/trio/recovery.py tests/test_trio_recovery_clarification.py
git commit -m "feat(trio): recovery — republish RESOLVED for tasks answered pre-crash"
```

---

## Phase 8: API + UI

### Task 17: PATCH `/api/repos/{id}/product-brief` endpoint

**Files:**
- Modify: `orchestrator/router.py`
- Test: `tests/test_repo_product_brief_endpoint.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_repo_product_brief_endpoint.py`:

```python
"""PATCH /api/repos/{id}/product-brief sets Repo.product_brief."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.router import update_repo_product_brief, ProductBriefIn
from shared.models import Repo


@pytest.mark.asyncio
async def test_patch_writes_product_brief():
    session = AsyncMock(spec=AsyncSession)
    repo = MagicMock(spec=Repo)
    repo.id = 5
    repo.product_brief = None
    with patch("orchestrator.router._get_repo_in_org",
               AsyncMock(return_value=repo)):
        out = await update_repo_product_brief(
            repo_id=5,
            body=ProductBriefIn(product_brief="# Mission"),
            session=session,
            org_id=1,
        )
    assert repo.product_brief == "# Mission"
    assert out.product_brief == "# Mission"


@pytest.mark.asyncio
async def test_patch_404_when_repo_missing():
    session = AsyncMock(spec=AsyncSession)
    with patch("orchestrator.router._get_repo_in_org",
               AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await update_repo_product_brief(
                repo_id=999,
                body=ProductBriefIn(product_brief="x"),
                session=session, org_id=1,
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_patch_accepts_empty_string_to_clear():
    session = AsyncMock(spec=AsyncSession)
    repo = MagicMock(spec=Repo)
    repo.id = 5
    repo.product_brief = "old"
    with patch("orchestrator.router._get_repo_in_org",
               AsyncMock(return_value=repo)):
        out = await update_repo_product_brief(
            repo_id=5,
            body=ProductBriefIn(product_brief=""),
            session=session, org_id=1,
        )
    # Empty string clears it (we treat empty as null on write).
    assert repo.product_brief is None
    assert out.product_brief is None
```

- [ ] **Step 2: Verify failure**

Run: `.venv/bin/python3 -m pytest tests/test_repo_product_brief_endpoint.py -v`
Expected: FAIL with `ImportError: cannot import name 'update_repo_product_brief'`.

- [ ] **Step 3: Add `ProductBriefIn` Pydantic model + endpoint**

In `orchestrator/router.py`, find an existing `class XxxRequest(BaseModel)` block for naming reference. Add near the other repo endpoints:

```python
class ProductBriefIn(BaseModel):
    product_brief: str = Field(default="", description="Markdown product brief.")


@router.patch(
    "/repos/{repo_id}/product-brief",
    response_model=RepoData,
)
async def update_repo_product_brief(
    repo_id: int,
    body: ProductBriefIn,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> RepoData:
    """Set or clear the repo's product brief.

    Empty string → stored as NULL (clears the brief). Org-scoped — only
    repos in the caller's org are visible.
    """
    repo = await _get_repo_in_org(session, repo_id=repo_id, org_id=org_id)
    if repo is None:
        raise HTTPException(404, "Repo not found")
    repo.product_brief = body.product_brief.strip() or None
    await session.commit()
    return RepoData(
        id=repo.id, name=repo.name, url=repo.url,
        default_branch=repo.default_branch,
        summary=repo.summary, summary_updated_at=repo.summary_updated_at,
        ci_checks=repo.ci_checks,
        harness_onboarded=repo.harness_onboarded or False,
        harness_pr_url=repo.harness_pr_url,
        product_brief=repo.product_brief,
    )
```

- [ ] **Step 4: Verify pass**

Run: `.venv/bin/python3 -m pytest tests/test_repo_product_brief_endpoint.py -v`
Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/router.py tests/test_repo_product_brief_endpoint.py
git commit -m "feat(api): PATCH /repos/{id}/product-brief endpoint"
```

---

### Task 18: Show clarification Q+A in `ArchitectAttemptsPanel`

**Files:**
- Modify: `web-next/components/trio/ArchitectAttemptsPanel.tsx`
- Test: manual UI smoke (no Jest in this codebase per recent commits — verify via dev server).

- [ ] **Step 1: Read the current panel**

Run: `cat web-next/components/trio/ArchitectAttemptsPanel.tsx`

Note the row structure — you'll be adding two markdown subsections (`Question`, `Answer`) inside each attempt row when the new fields are set.

- [ ] **Step 2: Update the component**

In `web-next/components/trio/ArchitectAttemptsPanel.tsx`, locate the per-attempt rendering. Add the following conditional rendering immediately after the existing `reasoning` preview:

```tsx
{attempt.clarification_question && (
  <div className="mt-2 rounded border-l-2 border-amber-500 pl-2">
    <div className="text-[10px] font-semibold uppercase text-amber-700">
      Question
    </div>
    <div className="whitespace-pre-wrap text-xs">
      {attempt.clarification_question}
    </div>
  </div>
)}
{attempt.clarification_answer && (
  <div className="mt-1 rounded border-l-2 border-emerald-500 pl-2">
    <div className="flex items-center gap-1 text-[10px] font-semibold uppercase text-emerald-700">
      Answer
      {attempt.clarification_source && (
        <span className="rounded bg-emerald-200/40 px-1 text-[9px]">
          {attempt.clarification_source.toUpperCase()}
        </span>
      )}
    </div>
    <div className="whitespace-pre-wrap text-xs">
      {attempt.clarification_answer}
    </div>
  </div>
)}
```

- [ ] **Step 3: Typecheck**

Run: `cd web-next && npx --no-install tsc --noEmit 2>&1 | tail -10`
Expected: no errors. (`clarification_question`/`clarification_answer`/`clarification_source` are now on the `ArchitectAttemptOut` interface from Task 3's TS regen.)

- [ ] **Step 4: Commit**

```bash
git add web-next/components/trio/ArchitectAttemptsPanel.tsx
git commit -m "feat(web-next): render architect clarification Q+A in attempts panel"
```

---

### Task 19: Add `product_brief` textarea to repo settings UI

**Files:**
- Modify: existing repo-settings form in `web-next` (path varies — find first)

- [ ] **Step 1: Find the existing repo settings form**

Run: `grep -rn "freeform\|RepoSettings\|po_goal" web-next/components web-next/app 2>&1 | head -10`

Open whichever file edits a repo's config / freeform settings — that's where the new textarea goes.

- [ ] **Step 2: Add the textarea**

In that component, add a labeled `<textarea>` for `product_brief`. Wire it to a TanStack Query mutation that calls `PATCH /api/repos/{id}/product-brief`:

```tsx
// In the form state:
const [productBrief, setProductBrief] = useState(repo.product_brief ?? '');

// In the API client (web-next/lib/repos.ts or wherever similar PATCHes live):
export async function updateProductBrief(repoId: number, productBrief: string) {
  return api<RepoData>(`/api/repos/${repoId}/product-brief`, {
    method: 'PATCH',
    body: JSON.stringify({ product_brief: productBrief }),
    headers: { 'Content-Type': 'application/json' },
  });
}

// In the form JSX:
<label className="block text-sm font-medium">Product brief</label>
<textarea
  className="mt-1 block w-full rounded border p-2 text-sm"
  rows={8}
  value={productBrief}
  onChange={(e) => setProductBrief(e.target.value)}
  placeholder="# Mission&#10;What does this repo build, for whom, and what are the non-goals?"
/>
<button
  onClick={() => updateMutation.mutate({ repoId: repo.id, productBrief })}
  className="mt-2 rounded bg-primary px-3 py-1 text-sm text-primary-foreground"
>
  Save product brief
</button>
```

Adapt to whatever component conventions the codebase uses (shadcn/ui `Textarea`/`Button` may already be imported).

- [ ] **Step 3: Typecheck**

Run: `cd web-next && npx --no-install tsc --noEmit 2>&1 | tail -10`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add web-next/
git commit -m "feat(web-next): product_brief textarea on repo settings"
```

---

## Phase 9: End-to-End Mocked Test

### Task 20: e2e mocked test for the clarification round-trip

**Files:**
- Test: `tests/test_trio_clarification_e2e_mocked.py` (new)

- [ ] **Step 1: Write the test**

Create `tests/test_trio_clarification_e2e_mocked.py`:

```python
"""End-to-end mocked: architect asks → PO answers → architect resumes →
backlog populated. LLM provider is mocked at the provider level so all
the seams (events, state machine, session persistence) run for real."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL for session fixture",
)
async def test_clarification_round_trip_populates_backlog(session, publisher):
    """Two architect LLM calls: first emits clarification, second emits backlog."""
    from shared.models import (
        Organization, Repo, Task, TaskComplexity,
        TaskSource, TaskStatus, TrioPhase,
    )

    org = Organization(name="e2e", slug="e2e")
    session.add(org)
    await session.flush()
    repo = Repo(
        name="r", url="https://github.com/o/r.git",
        organization_id=org.id, default_branch="main",
        product_brief="# Build a TODO app",
    )
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="Build something cool",
        source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
        freeform_mode=True, trio_phase=TrioPhase.ARCHITECTING,
    )
    session.add(parent)
    await session.commit()

    architect_outputs = iter([
        # Round 1: clarification.
        '```json\n{"decision": {"action": "awaiting_clarification", '
        '"question": "Pick framework?"}}\n```',
        # Round 2 (after answer arrives): backlog.
        '```json\n{"backlog": [{"id": "1", "title": "Init app", '
        '"description": "Scaffold"}]}\n```',
    ])

    def fake_arch_agent(*args, **kwargs):
        agent = MagicMock()
        agent.messages = []
        agent.api_messages = []
        agent.run = AsyncMock(side_effect=lambda *a, **kw: MagicMock(
            output=next(architect_outputs),
            messages=[], api_messages=[],
        ))
        return agent

    po_agent_mock = MagicMock()
    po_agent_mock.run = AsyncMock(return_value=MagicMock(
        output='```json\n{"answer": "React. Team knows it."}\n```'
    ))

    with patch("agent.lifecycle.trio.architect.create_architect_agent",
               side_effect=fake_arch_agent), \
         patch("agent.lifecycle.trio.architect._prepare_parent_workspace",
               AsyncMock(return_value=MagicMock(root="/tmp/ws-e2e"))), \
         patch("agent.lifecycle.trio.architect.home_dir_for_task",
               AsyncMock(return_value=None)), \
         patch("agent.lifecycle.trio.architect._commit_and_open_initial_pr",
               AsyncMock(return_value="cafe1234")), \
         patch("agent.session.Session.save", AsyncMock()), \
         patch("agent.session.Session.load",
               AsyncMock(return_value=([], []))), \
         patch("agent.po_agent.create_agent", return_value=po_agent_mock), \
         patch("agent.po_agent.clone_repo",
               AsyncMock(return_value=MagicMock(root="/tmp/po-ws"))):

        # 1. Architect first run — emits clarification.
        from agent.lifecycle.trio import architect
        await architect.run_initial(parent.id)

        # 2. Dispatcher fires PO (simulating the bus).
        from run import on_architect_clarification_needed
        from shared.events import Event, TaskEventType
        await on_architect_clarification_needed(Event(
            type=TaskEventType.ARCHITECT_CLARIFICATION_NEEDED,
            task_id=parent.id,
            payload={"question": "Pick framework?"},
        ))
        import asyncio
        await asyncio.sleep(0)
        # po_agent runs in a create_task — give it a tick.
        for _ in range(10):
            await session.refresh(parent)
            from shared.models import ArchitectAttempt
            from sqlalchemy import select as _sel
            attempt = (await session.execute(
                _sel(ArchitectAttempt).where(ArchitectAttempt.task_id == parent.id)
            )).scalar_one()
            if attempt.clarification_answer is not None:
                break
            await asyncio.sleep(0.05)

        # 3. RESOLVED handler runs — transition + resume.
        from run import on_architect_clarification_resolved
        await on_architect_clarification_resolved(Event(
            type=TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED,
            task_id=parent.id,
        ))
        # Give architect.resume a tick.
        for _ in range(10):
            await session.refresh(parent)
            if parent.trio_backlog is not None:
                break
            await asyncio.sleep(0.05)

    # Final state: TRIO_EXECUTING, backlog populated.
    await session.refresh(parent)
    assert parent.status == TaskStatus.TRIO_EXECUTING
    assert parent.trio_backlog is not None
    assert len(parent.trio_backlog) == 1
    assert parent.trio_backlog[0]["title"] == "Init app"
```

- [ ] **Step 2: Verify pass**

Run: `DATABASE_URL=... .venv/bin/python3 -m pytest tests/test_trio_clarification_e2e_mocked.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_trio_clarification_e2e_mocked.py
git commit -m "test(trio): end-to-end clarification round-trip with mocked LLMs"
```

---

## Phase 10: Deploy

### Task 21: Deploy + apply migration on the VM

**Files:** none (operational)

- [ ] **Step 1: Run the unit suite locally one last time**

Run: `.venv/bin/python3 -m pytest tests/ -q --no-header 2>&1 | tail -5`
Expected: all-green or the documented pre-existing 35 failures (no new ones).

- [ ] **Step 2: Lint**

Run: `.venv/bin/python3 -m ruff check . 2>&1 | tail -5`
Expected: no new errors introduced by this branch.

- [ ] **Step 3: Deploy**

Run: `./scripts/deploy.sh migrate`
Expected: builds both images, applies migration 034 on the VM, restarts containers, health check `{"status":"ok"}`.

- [ ] **Step 4: Verify migration on VM**

```bash
ssh azureuser@172.190.26.82 \
  "cd ~/auto-agent && docker compose exec -T postgres psql -U autoagent -d autoagent \
   -c 'SELECT version_num FROM alembic_version;'"
```
Expected: `034`.

```bash
ssh azureuser@172.190.26.82 \
  "cd ~/auto-agent && docker compose exec -T postgres psql -U autoagent -d autoagent \
   -c \"SELECT column_name FROM information_schema.columns \
        WHERE table_name = 'architect_attempts' \
        AND column_name LIKE 'clarif%' ORDER BY column_name;\""
```
Expected: 3 rows — `clarification_answer`, `clarification_question`, `clarification_source`.

```bash
ssh azureuser@172.190.26.82 \
  "cd ~/auto-agent && docker compose exec -T postgres psql -U autoagent -d autoagent \
   -c \"SELECT column_name FROM information_schema.columns \
        WHERE table_name = 'repos' AND column_name = 'product_brief';\""
```
Expected: 1 row.

- [ ] **Step 5: Smoke — re-test task #167**

```bash
# Set a product_brief on the iot-apartment-simulator repo (id 175) first
# so freeform has context.
ssh azureuser@172.190.26.82 \
  "cd ~/auto-agent && docker compose exec -T postgres psql -U autoagent -d autoagent \
   -c \"UPDATE repos SET product_brief = '<<TYPE PRODUCT BRIEF HERE>>' WHERE id = 175;\""
```

Then transition task #167 back to TRIO_EXECUTING (it's currently BLOCKED):

```bash
ssh azureuser@172.190.26.82 \
  "cd ~/auto-agent && docker compose exec -T auto-agent python3 -c '
import asyncio
from sqlalchemy import select
from shared.database import async_session
from shared.events import Event, TaskEventType, RedisStreamPublisher, set_publisher, publish
from shared.config import settings
from shared.models import Task, TaskStatus, TaskHistory

async def main():
    async with async_session() as s:
        t = (await s.execute(select(Task).where(Task.id == 167))).scalar_one()
        prev = t.status
        t.status = TaskStatus.QUEUED
        s.add(TaskHistory(task_id=167, from_status=prev, to_status=TaskStatus.QUEUED,
                          message=\"manual retry after clarification flow shipped\"))
        await s.commit()
    pub = RedisStreamPublisher(settings.redis_url); set_publisher(pub)
    await publish(Event(type=TaskEventType.START_QUEUED, task_id=167))
    await pub.aclose()

asyncio.run(main())
'"
```

Observe the architect's behavior — it should either:
- Emit a backlog cleanly (the new prompt should help), OR
- Emit `awaiting_clarification` with a single packaged question and have the PO answer it within ~30 seconds (visible in `architect_attempts` table).

- [ ] **Step 6: Commit any deploy-flagged fixes if needed, then close out**

If the smoke surfaces a bug, fix it as a follow-up task and amend the plan. Otherwise:

```bash
git log --oneline -22 | head -22
# Confirm the 21 task commits are present.
```

---

## Out of Scope (Reminders)

- Planner clarification flow stays untouched.
- Per-question answer UI (one combined answer per round in v1).
- Auto-approve PO answers / human-in-the-loop oversight of PO answers.
- Backfilling `product_brief` for existing repos.
- Adopting `product_brief` in `po_analyzer.run_po_analysis` suggestion prompts.

---

## Self-review

- [x] Spec coverage: all 8 acceptance criteria from the spec map to tasks 1–20. (Acceptance crit 6 — architect resume + answer injection — covered by tasks 11/12; crit 7 — UI visibility — covered by task 18.)
- [x] Placeholder scan: no TODO/TBD/FIXME inside task bodies. Single `<<TYPE PRODUCT BRIEF HERE>>` token in task 21 step 5 is a deliberate user-input marker, not a placeholder for the engineer.
- [x] Type consistency: `clarification_question`/`clarification_answer`/`clarification_source` used identically in models, types, tests, dispatcher, recovery, UI. `session_blob_path` only on the model (not in `ArchitectAttemptOut`). `ProductBriefIn` and `update_repo_product_brief` are referenced once each.
