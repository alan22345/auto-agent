# Multi-tenant Claude credentials + 5-worker concurrency — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each teammate authenticate their own Claude CLI subscription through the auto-agent web UI, run their tasks under their own credentials with no cross-user spill-over, and run up to 5 concurrent CLI workers (max 1 per repo).

**Architecture:** Each user gets a per-user vault directory at `/data/users/<user_id>/.claude/`. A new in-UI pairing flow drives `claude setup-token` inside a PTY (pseudo-terminal) so the CLI's interactive OAuth works without SSH. At task dispatch, the orchestrator passes `HOME=<vault>` to the CLI subprocess, isolating credentials per user. The queue becomes a single 5-slot global FIFO with a per-`repo_id` cap of 1 active task to prevent working-tree conflicts. A dispatch-time auth probe detects expired tokens and pauses tasks in a new `BLOCKED_ON_AUTH` state until the user reconnects.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Alembic, `ptyprocess` (new dep), Pydantic, pytest, Next.js (App Router) + TanStack Query for the Settings page.

**Spec:** `docs/superpowers/specs/2026-05-05-multi-tenant-claude-credentials-design.md`

---

## File Structure

**Backend (new):**
- `orchestrator/claude_pairing.py` — PTY manager + pairing-session registry. Owns lifecycle of `claude setup-token` subprocesses keyed by `pairing_id`.
- `orchestrator/claude_auth.py` — vault-path helper, dispatch-time auth probe, requeue-after-pair logic.
- `migrations/versions/022_multi_tenant_claude_credentials.py` — adds `users.claude_auth_status`, `users.claude_paired_at`, and `tasks.status='blocked_on_auth'`.
- `tests/test_claude_pairing.py`, `tests/test_claude_auth_probe.py`, `tests/test_queue_multi_tenant.py`, `tests/test_claude_cli_home_dir.py`.

**Backend (modified):**
- `shared/config.py` — replace `max_concurrent_simple/complex` with `max_concurrent_workers`; add `users_data_dir`.
- `shared/models.py` — add columns on `User`; add `BLOCKED_ON_AUTH` to `TaskStatus`.
- `orchestrator/queue.py` — single-pool with per-repo cap.
- `orchestrator/router.py` — pairing endpoints + auth-state field on `/auth/me`; reject task submits from unpaired users.
- `agent/llm/claude_cli.py` — accept `home_dir` and pass `env={**os.environ, "HOME": home_dir}` to subprocess.
- `agent/llm/__init__.py` — thread `home_dir` through `get_provider`.
- `agent/loop.py` — pass `home_dir` to provider in passthrough mode.
- `run.py` — pairing WebSocket route; update queue callers; emit `auth_required` events; thread `home_dir` from task → provider.

**Frontend (new):**
- `web-next/app/(app)/settings/claude/page.tsx` — Connect Claude page.
- `web-next/components/settings/connect-claude.tsx` — pairing UI component.
- `web-next/hooks/useClaudePairing.ts` — pairing-flow hook (start, stream, submit code, poll status).
- `web-next/lib/claude-pairing.ts` — typed API client.

**Frontend (modified):**
- `web-next/components/layout/sidebar.tsx` (or wherever auth status surfaces) — global "auth expired" banner when current user's `claude_auth_status != 'paired'`.

---

## Task 1: Add `ptyprocess` dependency

**Files:**
- Modify: `pyproject.toml` (or `requirements.txt` — verify which the project uses before editing).

- [ ] **Step 1: Confirm dep manifest**

Run: `ls pyproject.toml requirements.txt 2>/dev/null`
Expected: at least one of these prints. Use whichever exists. If both exist, `pyproject.toml` wins; check it for an existing `[project.dependencies]` or `[tool.poetry.dependencies]` section.

- [ ] **Step 2: Add `ptyprocess` to deps**

In `pyproject.toml` under `[project] dependencies` (or whatever block already lists `fastapi`, `sqlalchemy`, etc.), add:

```
"ptyprocess>=0.7.0",
```

If the project uses `requirements.txt`, append on its own line:

```
ptyprocess>=0.7.0
```

- [ ] **Step 3: Install**

Run: `.venv/bin/pip install 'ptyprocess>=0.7.0'`
Expected: `Successfully installed ptyprocess-...`

- [ ] **Step 4: Smoke-test the import**

Run: `.venv/bin/python3 -c "import ptyprocess; print(ptyprocess.__version__)"`
Expected: a version string like `0.7.0`. No traceback.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml requirements.txt 2>/dev/null
git commit -m "deps: add ptyprocess for in-UI Claude pairing flow"
```

---

## Task 2: Database migration — add user auth columns + new task status

**Files:**
- Create: `migrations/versions/022_multi_tenant_claude_credentials.py`

- [ ] **Step 1: Write the migration**

```python
"""Add per-user Claude auth columns and the BLOCKED_ON_AUTH task status.

Revision ID: 022
Revises: 021
Create Date: 2026-05-05
"""
from typing import Sequence, Union

from alembic import op


revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS claude_auth_status VARCHAR(32) "
        "NOT NULL DEFAULT 'never_paired'"
    )
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS claude_paired_at TIMESTAMPTZ NULL"
    )
    op.execute(
        "ALTER TABLE users "
        "ADD CONSTRAINT users_claude_auth_status_check "
        "CHECK (claude_auth_status IN ('paired', 'expired', 'never_paired'))"
    )
    # Extend the taskstatus enum used by tasks.status
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'blocked_on_auth'")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_claude_auth_status_check"
    )
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS claude_paired_at")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS claude_auth_status")
    # Note: removing an enum value in Postgres requires recreating the type.
    # Safe to leave 'blocked_on_auth' on the enum during downgrade — no rows
    # reference it once the orchestrator code is rolled back.
```

- [ ] **Step 2: Apply the migration locally**

Run: `docker compose exec auto-agent alembic upgrade head`
Expected: `INFO  [alembic.runtime.migration] Running upgrade 021 -> 022, ...`. No errors.

- [ ] **Step 3: Verify the schema**

Run:
```bash
docker compose exec auto-agent psql "$DATABASE_URL" -c "\d users" \
  | grep -E "claude_auth_status|claude_paired_at"
```
Expected: two lines showing both columns.

- [ ] **Step 4: Commit**

```bash
git add migrations/versions/022_multi_tenant_claude_credentials.py
git commit -m "migration: per-user Claude auth columns + BLOCKED_ON_AUTH status"
```

---

## Task 3: Update ORM models

**Files:**
- Modify: `shared/models.py:31-44` (TaskStatus enum) and `shared/models.py:244-253` (User model).

- [ ] **Step 1: Add the enum value to `TaskStatus`**

In `shared/models.py`, find the `TaskStatus` enum (line 31). Add one entry just before `BLOCKED`:

```python
    BLOCKED_ON_AUTH = "blocked_on_auth"
    BLOCKED = "blocked"
```

- [ ] **Step 2: Add columns to `User`**

In the `User` class definition (around line 244), after the existing `last_login` column, add:

```python
    claude_auth_status = Column(
        String(32), nullable=False, default="never_paired"
    )
    claude_paired_at = Column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 3: Update `ACTIVE_STATUSES` in queue.py to NOT include the new status**

`BLOCKED_ON_AUTH` should NOT count as an active slot — those tasks are paused, not running. Open `orchestrator/queue.py` and confirm the existing `ACTIVE_STATUSES` set does not need modification (it only enumerates currently-active states; the new status is a paused state, so no change).

Verification — read the file and confirm `BLOCKED_ON_AUTH` is absent from the set.

- [ ] **Step 4: Run the test suite to confirm nothing breaks**

Run: `.venv/bin/python3 -m pytest tests/ -q -x`
Expected: all tests pass. If any test references `User` row construction and now fails because the column has a non-null default, it's safe — the default applies at insert time. If a test fails, read the failure and fix only the test (not the model).

- [ ] **Step 5: Commit**

```bash
git add shared/models.py
git commit -m "models: add Claude auth columns to User and BLOCKED_ON_AUTH status"
```

---

## Task 4: Vault path helper

**Files:**
- Create: `orchestrator/claude_auth.py`
- Create: `tests/test_claude_auth_vault.py`
- Modify: `shared/config.py` (add `users_data_dir`)

- [ ] **Step 1: Add config setting**

In `shared/config.py`, inside the `Settings` class, add (place it near the other path-like settings):

```python
    # Root for per-user data. Each user's Claude credentials live at
    # f"{users_data_dir}/{user_id}/.claude/".
    users_data_dir: str = "/data/users"
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_claude_auth_vault.py`:

```python
import os
import stat

import pytest

from orchestrator.claude_auth import vault_dir_for, ensure_vault_dir


def test_vault_dir_for_returns_per_user_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.claude_auth.settings.users_data_dir", str(tmp_path)
    )
    assert vault_dir_for(42) == str(tmp_path / "42")


def test_ensure_vault_dir_creates_with_0700(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.claude_auth.settings.users_data_dir", str(tmp_path)
    )
    path = ensure_vault_dir(7)
    assert os.path.isdir(path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o700, f"expected 0700, got {oct(mode)}"


def test_ensure_vault_dir_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.claude_auth.settings.users_data_dir", str(tmp_path)
    )
    p1 = ensure_vault_dir(7)
    p2 = ensure_vault_dir(7)
    assert p1 == p2
```

- [ ] **Step 3: Run the test and confirm it fails**

Run: `.venv/bin/python3 -m pytest tests/test_claude_auth_vault.py -v`
Expected: ImportError on `orchestrator.claude_auth`.

- [ ] **Step 4: Implement the helpers**

Create `orchestrator/claude_auth.py`:

```python
"""Per-user Claude credential vault: path helpers and auth-state utilities."""
from __future__ import annotations

import os

from shared.config import settings


def vault_dir_for(user_id: int) -> str:
    """Return the absolute HOME directory for the user's Claude vault.

    The CLI looks for credentials at $HOME/.claude/.credentials.json, so we
    pass this path as HOME when spawning the subprocess.
    """
    return os.path.join(settings.users_data_dir, str(user_id))


def ensure_vault_dir(user_id: int) -> str:
    """Create the user's vault directory (mode 0700) if it doesn't exist.

    Returns the path. Idempotent.
    """
    path = vault_dir_for(user_id)
    os.makedirs(path, mode=0o700, exist_ok=True)
    # makedirs honors mode only on creation; enforce on existing dirs too.
    os.chmod(path, 0o700)
    return path
```

- [ ] **Step 5: Run the test and confirm it passes**

Run: `.venv/bin/python3 -m pytest tests/test_claude_auth_vault.py -v`
Expected: 3 tests pass.

- [ ] **Step 6: Commit**

```bash
git add shared/config.py orchestrator/claude_auth.py tests/test_claude_auth_vault.py
git commit -m "feat(claude_auth): per-user vault path helpers"
```

---

## Task 5: Drop legacy concurrency settings, add unified worker cap

**Files:**
- Modify: `shared/config.py:62-64`

- [ ] **Step 1: Replace the settings**

In `shared/config.py`, replace:

```python
    # Concurrency
    max_concurrent_simple: int = 1
    max_concurrent_complex: int = 1
```

with:

```python
    # Concurrency: a single global pool. Per-repo cap of 1 is enforced in
    # orchestrator/queue.py separately to prevent concurrent agents on the
    # same working tree.
    max_concurrent_workers: int = 5
```

- [ ] **Step 2: Confirm no surviving readers of the old fields**

Run: `grep -rn "max_concurrent_simple\|max_concurrent_complex" --include="*.py" .`
Expected: only matches inside this plan or the spec file. If any source file still references the old names, fix it as part of Task 6.

- [ ] **Step 3: Commit**

```bash
git add shared/config.py
git commit -m "config: replace simple/complex caps with max_concurrent_workers=5"
```

---

## Task 6: Rewrite the queue (single pool + per-repo cap)

**Files:**
- Modify: `orchestrator/queue.py` (full rewrite, ~60 lines).
- Create: `tests/test_queue_multi_tenant.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_queue_multi_tenant.py`:

```python
"""Single-pool concurrency with per-repo cap of 1 and BLOCKED_ON_AUTH exemption."""
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator import queue as q
from shared.models import Task, TaskComplexity, TaskStatus, Repo, TaskSource


@pytest.mark.asyncio
async def test_can_start_when_under_global_cap(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(q.settings, "max_concurrent_workers", 5)
    repo = Repo(name="r1", url="https://x"); db_session.add(repo); await db_session.flush()
    candidate = Task(
        title="t", source=TaskSource.MANUAL, repo_id=repo.id,
        status=TaskStatus.QUEUED, complexity=TaskComplexity.SIMPLE,
    )
    db_session.add(candidate); await db_session.flush()
    assert await q.can_start_task(db_session, candidate) is True


@pytest.mark.asyncio
async def test_blocked_when_global_cap_reached(db_session, monkeypatch):
    monkeypatch.setattr(q.settings, "max_concurrent_workers", 2)
    r1 = Repo(name="r1", url="x"); r2 = Repo(name="r2", url="x")
    db_session.add_all([r1, r2]); await db_session.flush()
    db_session.add_all([
        Task(title="a", source=TaskSource.MANUAL, repo_id=r1.id,
             status=TaskStatus.CODING, complexity=TaskComplexity.SIMPLE),
        Task(title="b", source=TaskSource.MANUAL, repo_id=r2.id,
             status=TaskStatus.CODING, complexity=TaskComplexity.SIMPLE),
    ])
    await db_session.flush()
    candidate = Task(
        title="c", source=TaskSource.MANUAL, repo_id=None,
        status=TaskStatus.QUEUED, complexity=TaskComplexity.SIMPLE,
    )
    db_session.add(candidate); await db_session.flush()
    assert await q.can_start_task(db_session, candidate) is False


@pytest.mark.asyncio
async def test_blocked_when_same_repo_already_active(db_session, monkeypatch):
    monkeypatch.setattr(q.settings, "max_concurrent_workers", 5)
    repo = Repo(name="r1", url="x"); db_session.add(repo); await db_session.flush()
    db_session.add(Task(
        title="active", source=TaskSource.MANUAL, repo_id=repo.id,
        status=TaskStatus.CODING, complexity=TaskComplexity.SIMPLE,
    ))
    await db_session.flush()
    candidate = Task(
        title="next", source=TaskSource.MANUAL, repo_id=repo.id,
        status=TaskStatus.QUEUED, complexity=TaskComplexity.SIMPLE,
    )
    db_session.add(candidate); await db_session.flush()
    assert await q.can_start_task(db_session, candidate) is False


@pytest.mark.asyncio
async def test_repoless_tasks_bypass_per_repo_cap(db_session, monkeypatch):
    monkeypatch.setattr(q.settings, "max_concurrent_workers", 5)
    db_session.add(Task(
        title="active", source=TaskSource.MANUAL, repo_id=None,
        status=TaskStatus.CODING, complexity=TaskComplexity.SIMPLE_NO_CODE,
    ))
    await db_session.flush()
    candidate = Task(
        title="next", source=TaskSource.MANUAL, repo_id=None,
        status=TaskStatus.QUEUED, complexity=TaskComplexity.SIMPLE_NO_CODE,
    )
    db_session.add(candidate); await db_session.flush()
    assert await q.can_start_task(db_session, candidate) is True


@pytest.mark.asyncio
async def test_blocked_on_auth_does_not_count_as_active(db_session, monkeypatch):
    monkeypatch.setattr(q.settings, "max_concurrent_workers", 1)
    repo = Repo(name="r1", url="x"); db_session.add(repo); await db_session.flush()
    db_session.add(Task(
        title="paused", source=TaskSource.MANUAL, repo_id=repo.id,
        status=TaskStatus.BLOCKED_ON_AUTH, complexity=TaskComplexity.SIMPLE,
    ))
    await db_session.flush()
    candidate = Task(
        title="next", source=TaskSource.MANUAL, repo_id=repo.id,
        status=TaskStatus.QUEUED, complexity=TaskComplexity.SIMPLE,
    )
    db_session.add(candidate); await db_session.flush()
    assert await q.can_start_task(db_session, candidate) is True


@pytest.mark.asyncio
async def test_next_eligible_skips_repo_blocked_tasks(db_session, monkeypatch):
    """A task on a busy repo must not head-of-line-block tasks on other repos."""
    monkeypatch.setattr(q.settings, "max_concurrent_workers", 5)
    r1 = Repo(name="r1", url="x"); r2 = Repo(name="r2", url="x")
    db_session.add_all([r1, r2]); await db_session.flush()
    db_session.add(Task(
        title="active-on-r1", source=TaskSource.MANUAL, repo_id=r1.id,
        status=TaskStatus.CODING, complexity=TaskComplexity.SIMPLE,
    ))
    db_session.add(Task(
        title="head-of-line", source=TaskSource.MANUAL, repo_id=r1.id,
        status=TaskStatus.QUEUED, complexity=TaskComplexity.SIMPLE, priority=100,
    ))
    db_session.add(Task(
        title="should-run", source=TaskSource.MANUAL, repo_id=r2.id,
        status=TaskStatus.QUEUED, complexity=TaskComplexity.SIMPLE, priority=100,
    ))
    await db_session.flush()
    next_task = await q.next_eligible_task(db_session)
    assert next_task is not None
    assert next_task.title == "should-run"
```

> **Note for the implementer:** the project's pytest setup may not yet have a `db_session` fixture exposing an `AsyncSession`. Check `tests/conftest.py`. If a fixture by another name (e.g. `session`, `async_session`) exists, rename `db_session` in the test file to match. If no async-DB fixture exists, add one to `tests/conftest.py` modeled on the existing test patterns — do NOT invent a new style of fixture.

- [ ] **Step 2: Run the test and confirm it fails**

Run: `.venv/bin/python3 -m pytest tests/test_queue_multi_tenant.py -v`
Expected: failure on missing `q.can_start_task` and `q.next_eligible_task`.

- [ ] **Step 3: Rewrite `orchestrator/queue.py`**

Replace the file's entire contents with:

```python
"""Task queue — single global pool with per-repo cap.

Concurrency rules:
  - At most ``settings.max_concurrent_workers`` tasks active at once.
  - At most 1 active task per repo_id (prevents working-tree conflicts).
  - Tasks with repo_id IS NULL (e.g. SIMPLE_NO_CODE research) bypass the
    per-repo cap; only the global cap applies.
  - BLOCKED_ON_AUTH is paused, not active — does not occupy a slot.

FIFO across all users. Priority (lower = first) breaks ties; default 100.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import settings
from shared.models import Task, TaskStatus

# Statuses that count as "active" (occupying a slot)
ACTIVE_STATUSES = {
    TaskStatus.PLANNING,
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.AWAITING_CLARIFICATION,
    TaskStatus.CODING,
    TaskStatus.PR_CREATED,
    TaskStatus.AWAITING_CI,
    TaskStatus.AWAITING_REVIEW,
    TaskStatus.BLOCKED,
}


async def count_active(session: AsyncSession) -> int:
    """Total active tasks across all users and repos."""
    result = await session.execute(
        select(func.count(Task.id)).where(Task.status.in_(ACTIVE_STATUSES))
    )
    return result.scalar_one()


async def _repo_has_active_task(session: AsyncSession, repo_id: int) -> bool:
    result = await session.execute(
        select(func.count(Task.id)).where(
            Task.repo_id == repo_id,
            Task.status.in_(ACTIVE_STATUSES),
        )
    )
    return result.scalar_one() > 0


async def can_start_task(session: AsyncSession, task: Task) -> bool:
    """Can this specific task start right now?"""
    if await count_active(session) >= settings.max_concurrent_workers:
        return False
    if task.repo_id is not None and await _repo_has_active_task(session, task.repo_id):
        return False
    return True


async def next_eligible_task(session: AsyncSession) -> Task | None:
    """Return the highest-priority QUEUED task that can start right now.

    Iterates queued tasks in (priority asc, created_at asc) order and returns
    the first one that passes can_start_task. A repo-blocked task is skipped
    so other repos' tasks aren't head-of-line-blocked.
    """
    if await count_active(session) >= settings.max_concurrent_workers:
        return None

    # Snapshot all currently-active repo_ids in one query.
    active_repos_q = await session.execute(
        select(Task.repo_id)
        .where(Task.status.in_(ACTIVE_STATUSES), Task.repo_id.is_not(None))
        .distinct()
    )
    busy_repos = {row[0] for row in active_repos_q.all()}

    queued_q = await session.execute(
        select(Task)
        .where(Task.status == TaskStatus.QUEUED)
        .order_by(Task.priority.asc(), Task.created_at.asc())
    )
    for t in queued_q.scalars():
        if t.repo_id is None or t.repo_id not in busy_repos:
            return t
    return None
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `.venv/bin/python3 -m pytest tests/test_queue_multi_tenant.py -v`
Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/queue.py tests/test_queue_multi_tenant.py
git commit -m "feat(queue): single 5-slot pool with per-repo cap of 1"
```

---

## Task 7: Update queue callers in `run.py`

**Files:**
- Modify: `run.py` — three call sites at lines ~271, ~916-948, ~958.

- [ ] **Step 1: Update the import**

In `run.py` line 40, change:

```python
from orchestrator.queue import can_start, next_queued_task
```

to:

```python
from orchestrator.queue import can_start_task, next_eligible_task
```

- [ ] **Step 2: Update `on_task_classified` (line ~271)**

Replace `await can_start(session, task.complexity)` with `await can_start_task(session, task)`.

- [ ] **Step 3: Rewrite `_try_start_queued` (lines ~916-948)**

Replace the function body with a single-pass version:

```python
async def _try_start_queued(session) -> None:
    """Start one queued task if a slot is available. Repeats until full."""
    while True:
        task = await next_eligible_task(session)
        if task is None:
            return

        # Respect freeform toggle
        if task.freeform_mode and task.repo_id:
            from sqlalchemy import select as _sel
            cfg_result = await session.execute(
                _sel(FreeformConfig).where(FreeformConfig.repo_id == task.repo_id)
            )
            cfg = cfg_result.scalar_one_or_none()
            if not cfg or not cfg.enabled:
                log.info(f"Skipping freeform task #{task.id}: repo freeform disabled")
                # Demote priority temporarily so we don't loop on it.
                # Simpler: just return — it'll be reconsidered next event.
                return

        if task.complexity in (TaskComplexity.COMPLEX, TaskComplexity.COMPLEX_LARGE):
            task = await transition(session, task, TaskStatus.PLANNING, "Slot opened, starting planning")
            await session.commit()
            await publish(task_start_planning(task.id))
        else:
            task = await transition(session, task, TaskStatus.CODING, "Slot opened, starting coding")
            await session.commit()
            await publish(task_start_coding(task.id))
```

- [ ] **Step 4: Update `on_start_queued_task` (line ~958)**

Replace `await can_start(session, task.complexity)` with `await can_start_task(session, task)`.

- [ ] **Step 5: Run unit tests**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: all pass. If any test imported `can_start` or `next_queued_task` directly, update them.

- [ ] **Step 6: Commit**

```bash
git add run.py
git commit -m "refactor(run): use single-pool queue API (can_start_task / next_eligible_task)"
```

---

## Task 8: Thread `home_dir` through the CLI provider

**Files:**
- Modify: `agent/llm/claude_cli.py:38-46` (constructor + setters) and `:89-109` (subprocess invocation).
- Modify: `agent/llm/__init__.py:55-58` (provider factory).
- Modify: `agent/loop.py:162-177` (passthrough).
- Create: `tests/test_claude_cli_home_dir.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_claude_cli_home_dir.py`:

```python
"""ClaudeCLIProvider must spawn the subprocess with HOME=<vault_dir>."""
from unittest.mock import AsyncMock, patch

import pytest

from agent.llm.claude_cli import ClaudeCLIProvider


@pytest.mark.asyncio
async def test_invoke_passes_home_env(tmp_path):
    provider = ClaudeCLIProvider()
    provider.set_home_dir(str(tmp_path))

    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(return_value=(b"hello", b""))
    fake_proc.returncode = 0

    with patch(
        "agent.llm.claude_cli.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ) as spawn:
        await provider._invoke_cli_once("ping")

    kwargs = spawn.call_args.kwargs
    assert "env" in kwargs, "subprocess must receive env"
    assert kwargs["env"]["HOME"] == str(tmp_path)


@pytest.mark.asyncio
async def test_invoke_without_home_dir_inherits_env(tmp_path, monkeypatch):
    provider = ClaudeCLIProvider()
    monkeypatch.setenv("HOME", "/root")

    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(return_value=(b"hi", b""))
    fake_proc.returncode = 0

    with patch(
        "agent.llm.claude_cli.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ) as spawn:
        await provider._invoke_cli_once("ping")

    # When home_dir is unset, we don't override; subprocess inherits parent env.
    kwargs = spawn.call_args.kwargs
    assert "env" not in kwargs or kwargs["env"] is None
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `.venv/bin/python3 -m pytest tests/test_claude_cli_home_dir.py -v`
Expected: AttributeError on `set_home_dir`.

- [ ] **Step 3: Edit `agent/llm/claude_cli.py` constructor + add setter**

Find lines 38-46 (the `__init__` and `set_cwd`/`set_session` block) and add a `_home_dir` field plus a setter:

```python
    def __init__(self, timeout: int = 1200, home_dir: str | None = None):
        self._timeout = timeout
        self._session_id: str | None = None
        self._cwd: str | None = None
        self._home_dir: str | None = home_dir

    def set_cwd(self, cwd: str) -> None:
        """Set the working directory for CLI invocations."""
        self._cwd = cwd

    def set_home_dir(self, home_dir: str) -> None:
        """Set HOME for CLI invocations — selects the user's credential vault."""
        self._home_dir = home_dir
```

- [ ] **Step 4: Edit `_invoke_cli_once` to pass env**

Find the `create_subprocess_exec` call (currently at line 104) and change it to:

```python
        kwargs = dict(
            cwd=self._cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if self._home_dir is not None:
            import os
            kwargs["env"] = {**os.environ, "HOME": self._home_dir}

        proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
```

- [ ] **Step 5: Run the test and confirm it passes**

Run: `.venv/bin/python3 -m pytest tests/test_claude_cli_home_dir.py -v`
Expected: 2 tests pass.

- [ ] **Step 6: Plumb `home_dir` through the provider factory**

In `agent/llm/__init__.py`, change `get_provider` to accept a `home_dir`:

```python
def get_provider(
    model_override: str | None = None,
    provider_override: str | None = None,
    home_dir: str | None = None,
) -> LLMProvider:
```

And in the `claude_cli` branch (around line 55):

```python
    elif provider == "claude_cli":
        from agent.llm.claude_cli import ClaudeCLIProvider

        return ClaudeCLIProvider(home_dir=home_dir)
```

The other provider branches ignore `home_dir` (it's CLI-only).

- [ ] **Step 7: Set home_dir in the agent loop's passthrough mode**

In `agent/loop.py`, find `_run_passthrough` (line ~162). Update it so that if `self._home_dir` is set on the loop, it's applied to the provider before calling `complete`. The loop already calls `set_cwd`; add right after:

```python
        if isinstance(self._provider, ClaudeCLIProvider):
            self._provider.set_cwd(self._workspace)
            if getattr(self, "_home_dir", None):
                self._provider.set_home_dir(self._home_dir)
            if self._session:
                self._provider.set_session(self._session.session_id, resume=resume)
```

And accept `home_dir` in the `AgentLoop.__init__`. Search the file for `__init__` and add `home_dir: str | None = None` as a kwarg, storing `self._home_dir = home_dir`.

- [ ] **Step 8: Run all tests**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: all pass. If `tests/test_claude_cli_session_recovery.py` fails because the constructor signature changed, the new `home_dir=None` default keeps it backward-compatible — re-read the failure if anything breaks.

- [ ] **Step 9: Commit**

```bash
git add agent/llm/claude_cli.py agent/llm/__init__.py agent/loop.py tests/test_claude_cli_home_dir.py
git commit -m "feat(claude_cli): per-user HOME for credential isolation"
```

---

## Task 9: Wire `home_dir` from the dispatching task into the agent loop

**Files:**
- Modify: `agent/main.py` — find the function(s) that construct an `AgentLoop` for a task (search for `AgentLoop(`).

- [ ] **Step 1: Locate AgentLoop construction sites**

Run: `grep -n "AgentLoop(" agent/main.py`
Expected: one or more matches. Read each call site and the function it lives in.

- [ ] **Step 2: For each construction site, look up the task's `created_by_user_id` and pass the vault dir**

At every `AgentLoop(...)` construction in `agent/main.py`, change to:

```python
from orchestrator.claude_auth import vault_dir_for, ensure_vault_dir

home_dir = None
if task.created_by_user_id is not None:
    home_dir = ensure_vault_dir(task.created_by_user_id)

loop = AgentLoop(
    # ...existing args...,
    home_dir=home_dir,
)
```

If a particular call site is for a flow that has no user (e.g. system-driven PO analysis), it's fine to leave `home_dir=None` — that path keeps the legacy behavior of inheriting the container's HOME. (Bedrock-backed flows don't read HOME at all.)

- [ ] **Step 3: Run unit tests**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add agent/main.py
git commit -m "feat(agent): dispatch tasks with per-user HOME for the CLI provider"
```

---

## Task 10: Pairing-session manager (PTY-driven `claude setup-token`)

**Files:**
- Create: `orchestrator/claude_pairing.py`
- Create: `tests/test_claude_pairing.py`

- [ ] **Step 1: Write the failing test (uses a fake `claude` script)**

Create `tests/test_claude_pairing.py`:

```python
"""End-to-end pairing flow with a fake `claude` binary in a PTY."""
import asyncio
import os
import stat
from pathlib import Path

import pytest

from orchestrator import claude_pairing as cp


FAKE_CLAUDE = """\
#!/usr/bin/env bash
set -e
echo "Open this URL in your browser:"
echo "https://claude.ai/login?code=fake-pairing-token-abc"
echo "Paste the code here:"
read code
mkdir -p "$HOME/.claude"
echo '{"token":"got-'"$code"'"}' > "$HOME/.claude/.credentials.json"
echo "Login successful."
"""


@pytest.fixture
def fake_claude_on_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return bin_dir


@pytest.mark.asyncio
async def test_pairing_full_round_trip(fake_claude_on_path, tmp_path, monkeypatch):
    monkeypatch.setattr(cp.settings, "users_data_dir", str(tmp_path / "vaults"))

    session = await cp.start_pairing(user_id=42)

    # Drain the URL line within a short window.
    url_seen = None
    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        line = await session.read_line(timeout=0.5)
        if line and "https://claude.ai" in line:
            url_seen = line.strip()
            break
    assert url_seen is not None, "fake claude never emitted a URL"

    await session.submit_code("CODE-123")

    result = await session.wait_for_exit(timeout=5.0)
    assert result.success is True, f"stderr={result.stderr}"

    cred = Path(tmp_path) / "vaults" / "42" / ".claude" / ".credentials.json"
    assert cred.exists()
    assert "got-CODE-123" in cred.read_text()


@pytest.mark.asyncio
async def test_pairing_session_registry_ttl(fake_claude_on_path, tmp_path, monkeypatch):
    monkeypatch.setattr(cp.settings, "users_data_dir", str(tmp_path / "vaults"))
    session = await cp.start_pairing(user_id=1)
    looked_up = cp.get_pairing(session.pairing_id)
    assert looked_up is session
    await session.cancel()
    assert cp.get_pairing(session.pairing_id) is None
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `.venv/bin/python3 -m pytest tests/test_claude_pairing.py -v`
Expected: ImportError on `orchestrator.claude_pairing`.

- [ ] **Step 3: Implement the pairing manager**

Create `orchestrator/claude_pairing.py`:

```python
"""PTY-driven Claude OAuth pairing.

Spawns `claude setup-token` inside a pseudo-terminal so the CLI's interactive
login flow works without a real TTY. The login URL is read off stdout and
forwarded to the browser; the user's pasted one-time code is written back
into the PTY's stdin. On success, the CLI writes credentials into the
user's vault directory ($HOME/.claude/.credentials.json) and exits 0.

Sessions are kept in an in-process registry keyed by a UUID `pairing_id`,
with a 5-minute TTL.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import ptyprocess

from orchestrator.claude_auth import ensure_vault_dir
from shared.config import settings

log = logging.getLogger(__name__)

PAIRING_TTL_SECONDS = 300
PAIRING_COMMAND = ["claude", "setup-token"]


@dataclass
class PairingResult:
    success: bool
    stderr: str
    exit_code: int


class PairingSession:
    def __init__(self, user_id: int, home_dir: str):
        self.pairing_id = str(uuid.uuid4())
        self.user_id = user_id
        self.home_dir = home_dir
        self.created_at = time.time()
        env = {**os.environ, "HOME": home_dir}
        self._proc = ptyprocess.PtyProcess.spawn(
            PAIRING_COMMAND, env=env, cwd=home_dir
        )
        self._buffer = ""
        self._closed = False

    async def read_line(self, timeout: float = 1.0) -> Optional[str]:
        """Read one line from the PTY, with timeout. Returns None on timeout."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if "\n" in self._buffer:
                line, _, rest = self._buffer.partition("\n")
                self._buffer = rest
                return line + "\n"
            try:
                chunk = await loop.run_in_executor(
                    None, self._proc.read, 1024
                )
            except (EOFError, OSError):
                self._closed = True
                return self._buffer or None
            if not chunk:
                await asyncio.sleep(0.05)
                continue
            self._buffer += chunk if isinstance(chunk, str) else chunk.decode(
                "utf-8", errors="replace"
            )
        return None

    async def submit_code(self, code: str) -> None:
        """Write the user's pasted one-time code into the PTY's stdin."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._proc.write, code + "\n")

    async def wait_for_exit(self, timeout: float = 30.0) -> PairingResult:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if not self._proc.isalive():
                break
            await asyncio.sleep(0.1)
        if self._proc.isalive():
            self._proc.kill(9)
            return PairingResult(False, "pairing timed out", -1)

        # Drain remaining output for stderr-style reporting.
        try:
            tail = self._proc.read()
            if tail:
                self._buffer += tail if isinstance(tail, str) else tail.decode(
                    "utf-8", errors="replace"
                )
        except (EOFError, OSError):
            pass

        exit_code = self._proc.exitstatus or 0
        cred_path = os.path.join(self.home_dir, ".claude", ".credentials.json")
        success = exit_code == 0 and os.path.exists(cred_path)
        _registry.pop(self.pairing_id, None)
        return PairingResult(
            success=success,
            stderr="" if success else self._buffer[-500:],
            exit_code=exit_code,
        )

    async def cancel(self) -> None:
        if self._proc.isalive():
            self._proc.kill(9)
        _registry.pop(self.pairing_id, None)


_registry: dict[str, PairingSession] = {}


async def start_pairing(user_id: int) -> PairingSession:
    home_dir = ensure_vault_dir(user_id)
    _gc_expired()
    session = PairingSession(user_id, home_dir)
    _registry[session.pairing_id] = session
    return session


def get_pairing(pairing_id: str) -> Optional[PairingSession]:
    _gc_expired()
    return _registry.get(pairing_id)


def _gc_expired() -> None:
    now = time.time()
    for pid, sess in list(_registry.items()):
        if now - sess.created_at > PAIRING_TTL_SECONDS:
            try:
                if sess._proc.isalive():
                    sess._proc.kill(9)
            except Exception:
                pass
            _registry.pop(pid, None)
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `.venv/bin/python3 -m pytest tests/test_claude_pairing.py -v`
Expected: 2 tests pass. If the fake-`claude` shell script fails to read from stdin under PTY, double-check the script has executable permissions and that `read code` returns when a newline is sent.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/claude_pairing.py tests/test_claude_pairing.py
git commit -m "feat(claude_pairing): PTY-driven Claude setup-token wrapper"
```

---

## Task 11: Pairing HTTP endpoints + WebSocket

**Files:**
- Modify: `orchestrator/router.py` — add 4 endpoints.
- Modify: `run.py:190-193` — register a second WebSocket route for pairing streams.

- [ ] **Step 1: Add pairing endpoints to `orchestrator/router.py`**

Append to the bottom of the router file:

```python
# ---- Claude pairing -----------------------------------------------------

from pydantic import BaseModel as _BM  # local alias to avoid clobbering imports
from orchestrator import claude_pairing
from sqlalchemy import update as _update
from datetime import datetime as _dt, timezone as _tz


class _PairStartResponse(_BM):
    pairing_id: str


class _PairCodeBody(_BM):
    pairing_id: str
    code: str


class _PairStatusResponse(_BM):
    claude_auth_status: str
    claude_paired_at: _dt | None


@router.post("/api/claude/pair/start", response_model=_PairStartResponse)
async def claude_pair_start(
    auto_agent_session: str | None = Cookie(default=None),
    authorization: str | None = Header(None),
) -> _PairStartResponse:
    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    session = await claude_pairing.start_pairing(user_id=payload["user_id"])
    return _PairStartResponse(pairing_id=session.pairing_id)


@router.post("/api/claude/pair/code")
async def claude_pair_code(
    body: _PairCodeBody,
    auto_agent_session: str | None = Cookie(default=None),
    authorization: str | None = Header(None),
) -> dict:
    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    sess = claude_pairing.get_pairing(body.pairing_id)
    if sess is None or sess.user_id != payload["user_id"]:
        raise HTTPException(status_code=404, detail="pairing session not found")
    await sess.submit_code(body.code)
    result = await sess.wait_for_exit(timeout=30.0)
    if not result.success:
        raise HTTPException(status_code=400, detail=result.stderr or "pairing failed")

    # Mark user paired and requeue any blocked-on-auth tasks they own.
    async with async_session() as s:
        await s.execute(
            _update(User)
            .where(User.id == payload["user_id"])
            .values(
                claude_auth_status="paired",
                claude_paired_at=_dt.now(_tz.utc),
            )
        )
        # Move blocked_on_auth → queued for this user's tasks.
        await s.execute(
            _update(Task)
            .where(
                Task.created_by_user_id == payload["user_id"],
                Task.status == TaskStatus.BLOCKED_ON_AUTH,
            )
            .values(status=TaskStatus.QUEUED)
        )
        await s.commit()
    await publish(Event(type="claude_pair_succeeded", task_id=None))
    return {"ok": True}


@router.get("/api/claude/pair/status", response_model=_PairStatusResponse)
async def claude_pair_status(
    auto_agent_session: str | None = Cookie(default=None),
    authorization: str | None = Header(None),
) -> _PairStatusResponse:
    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    async with async_session() as s:
        result = await s.execute(select(User).where(User.id == payload["user_id"]))
        user = result.scalar_one()
    return _PairStatusResponse(
        claude_auth_status=user.claude_auth_status,
        claude_paired_at=user.claude_paired_at,
    )


@router.post("/api/claude/pair/disconnect")
async def claude_pair_disconnect(
    auto_agent_session: str | None = Cookie(default=None),
    authorization: str | None = Header(None),
) -> dict:
    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    import shutil
    from orchestrator.claude_auth import vault_dir_for
    vault = vault_dir_for(payload["user_id"])
    claude_subdir = os.path.join(vault, ".claude")
    if os.path.isdir(claude_subdir):
        shutil.rmtree(claude_subdir)
    async with async_session() as s:
        await s.execute(
            _update(User)
            .where(User.id == payload["user_id"])
            .values(claude_auth_status="never_paired", claude_paired_at=None)
        )
        await s.commit()
    return {"ok": True}
```

> **Implementer note:** verify the imports at the top of `orchestrator/router.py` already cover `select`, `Event`, `publish`, `Task`, `TaskStatus`, `User`, `async_session`, `HTTPException`, `os`. If any are missing, add them — don't duplicate existing imports.

- [ ] **Step 2: Add the pairing WebSocket in `run.py`**

After the existing `@app.websocket("/ws")` block (around line 190), add:

```python
@app.websocket("/ws/claude/pair/{pairing_id}")
async def ws_claude_pair(ws: WebSocket, pairing_id: str) -> None:
    """Stream PTY stdout for the named pairing session to the browser.

    Auth is via ?token= query param (same pattern as the main /ws).
    """
    from orchestrator.auth import verify_token
    from orchestrator import claude_pairing

    token = ws.query_params.get("token")
    payload = verify_token(token) if token else None
    if not payload:
        await ws.close(code=4401)
        return

    sess = claude_pairing.get_pairing(pairing_id)
    if not sess or sess.user_id != payload["user_id"]:
        await ws.close(code=4404)
        return

    await ws.accept()
    try:
        while True:
            line = await sess.read_line(timeout=0.5)
            if line is None:
                if not sess._proc.isalive():
                    await ws.send_json({"type": "exit"})
                    return
                continue
            await ws.send_json({"type": "line", "text": line})
    except Exception as e:
        log.warning("pairing ws error: %s", e)
        try:
            await ws.close()
        except Exception:
            pass
```

- [ ] **Step 3: Manual smoke test**

Start the app: `python run.py` (or `docker compose up -d`).

In a browser dev console (logged in), run:

```js
const r = await fetch("/api/claude/pair/start", {method: "POST"});
const {pairing_id} = await r.json();
const ws = new WebSocket(`ws://localhost:8000/ws/claude/pair/${pairing_id}?token=...`);
ws.onmessage = e => console.log(e.data);
```

Expected: lines stream in including a `https://claude.ai/...` URL.

- [ ] **Step 4: Commit**

```bash
git add orchestrator/router.py run.py
git commit -m "feat(api): Claude pairing endpoints + WebSocket stream"
```

---

## Task 12: Dispatch-time auth probe + reject-unpaired-on-submit

**Files:**
- Modify: `orchestrator/claude_auth.py` (add `probe_credentials`).
- Modify: `run.py` — `on_task_classified` and `_try_start_queued`: probe before transitioning to PLANNING/CODING.
- Modify: `orchestrator/router.py` — task-creation endpoint: 400 if `claude_auth_status != 'paired'`.
- Create: `tests/test_claude_auth_probe.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_claude_auth_probe.py`:

```python
"""Auth probe classifies stderr signatures into paired/expired."""
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator import claude_auth


@pytest.mark.asyncio
async def test_probe_paired_on_clean_exit(tmp_path):
    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(return_value=(b"hi", b""))
    fake_proc.returncode = 0
    with patch(
        "orchestrator.claude_auth.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        status = await claude_auth.probe_credentials(str(tmp_path))
    assert status == "paired"


@pytest.mark.asyncio
async def test_probe_expired_on_unauthorized_stderr(tmp_path):
    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(
        return_value=(b"", b"Error: unauthorized — please log in again.")
    )
    fake_proc.returncode = 1
    with patch(
        "orchestrator.claude_auth.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ):
        status = await claude_auth.probe_credentials(str(tmp_path))
    assert status == "expired"
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `.venv/bin/python3 -m pytest tests/test_claude_auth_probe.py -v`
Expected: AttributeError on `probe_credentials`.

- [ ] **Step 3: Add `probe_credentials` to `orchestrator/claude_auth.py`**

Append:

```python
import asyncio
import os
from typing import Literal

_AUTH_FAILURE_PATTERNS = (
    "unauthorized",
    "expired",
    "please log in",
    "not logged in",
    "authentication required",
)


async def probe_credentials(home_dir: str, timeout: float = 15.0) -> Literal["paired", "expired"]:
    """Run a minimal `claude` invocation under the given HOME and classify result.

    Returns "paired" on clean exit, "expired" if stderr matches an auth-failure
    pattern. Any other failure (timeout, missing binary, non-auth error) is
    treated as "expired" — better to ask the user to re-pair than silently fail
    a task at run time.
    """
    env = {**os.environ, "HOME": home_dir}
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "--print", "--dangerously-skip-permissions", "ping",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, FileNotFoundError):
        return "expired"

    if proc.returncode == 0:
        return "paired"
    err = (stderr or b"").decode("utf-8", errors="replace").lower()
    if any(p in err for p in _AUTH_FAILURE_PATTERNS):
        return "expired"
    # Non-auth failure: still treat as expired so the user is prompted, rather
    # than us inferring the credential is fine when we couldn't confirm it.
    return "expired"
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `.venv/bin/python3 -m pytest tests/test_claude_auth_probe.py -v`
Expected: 2 tests pass.

- [ ] **Step 5: Apply the probe at dispatch time in `run.py`**

In `on_task_classified` (around line 271), before the `if force_start or await can_start_task(...)` branch, add:

```python
        if task.created_by_user_id is not None and task.complexity != TaskComplexity.SIMPLE_NO_CODE:
            from orchestrator.claude_auth import probe_credentials, ensure_vault_dir
            from sqlalchemy import update as _u
            home = ensure_vault_dir(task.created_by_user_id)
            status = await probe_credentials(home)
            if status == "expired":
                await session.execute(
                    _u(User).where(User.id == task.created_by_user_id)
                    .values(claude_auth_status="expired")
                )
                task = await transition(
                    session, task, TaskStatus.BLOCKED_ON_AUTH,
                    "Claude credentials expired — user must reconnect",
                )
                await session.commit()
                await publish(Event(type="claude_auth_required", task_id=task.id))
                return
```

Apply the same guard inside `_try_start_queued` immediately before the transition to PLANNING/CODING.

- [ ] **Step 6: Reject task creation from unpaired users**

In `orchestrator/router.py`, find the task-creation endpoint (search for `created_by_user_id=req.created_by_user_id` around line 324). Just before the task is inserted, add:

```python
        if req.created_by_user_id is not None:
            user_q = await session.execute(
                select(User).where(User.id == req.created_by_user_id)
            )
            user = user_q.scalar_one_or_none()
            if user and user.claude_auth_status != "paired":
                raise HTTPException(
                    status_code=400,
                    detail="Connect your Claude account in Settings before queuing tasks.",
                )
```

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add orchestrator/claude_auth.py orchestrator/router.py run.py tests/test_claude_auth_probe.py
git commit -m "feat(claude_auth): dispatch-time probe + reject unpaired submits"
```

---

## Task 13: Surface auth status on `/auth/me`

**Files:**
- Modify: `orchestrator/router.py` — `/auth/me` response (around line 225-244).

- [ ] **Step 1: Extend the `/auth/me` response**

Find `/auth/me`. Whatever Pydantic model it currently returns, add two fields:

```python
    claude_auth_status: str
    claude_paired_at: datetime | None
```

And populate them from the loaded `User` row.

- [ ] **Step 2: Run tests**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/router.py
git commit -m "feat(api): expose claude_auth_status on /auth/me"
```

---

## Task 14: web-next Settings → Connect Claude page

**Files:**
- Create: `web-next/app/(app)/settings/claude/page.tsx`
- Create: `web-next/components/settings/connect-claude.tsx`
- Create: `web-next/hooks/useClaudePairing.ts`
- Create: `web-next/lib/claude-pairing.ts`

- [ ] **Step 1: Typed API client**

Create `web-next/lib/claude-pairing.ts`:

```typescript
export type ClaudeAuthStatus = "paired" | "expired" | "never_paired";

export interface PairStatus {
  claude_auth_status: ClaudeAuthStatus;
  claude_paired_at: string | null;
}

export async function startPairing(): Promise<{ pairing_id: string }> {
  const r = await fetch("/api/claude/pair/start", {
    method: "POST",
    credentials: "include",
  });
  if (!r.ok) throw new Error(`pair start failed: ${r.status}`);
  return r.json();
}

export async function submitPairCode(
  pairing_id: string,
  code: string,
): Promise<void> {
  const r = await fetch("/api/claude/pair/code", {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ pairing_id, code }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detail.detail ?? `pair code failed: ${r.status}`);
  }
}

export async function getPairStatus(): Promise<PairStatus> {
  const r = await fetch("/api/claude/pair/status", { credentials: "include" });
  if (!r.ok) throw new Error(`pair status failed: ${r.status}`);
  return r.json();
}

export async function disconnectClaude(): Promise<void> {
  await fetch("/api/claude/pair/disconnect", {
    method: "POST",
    credentials: "include",
  });
}
```

- [ ] **Step 2: React hook that owns the WebSocket lifecycle**

Create `web-next/hooks/useClaudePairing.ts`:

```typescript
import { useCallback, useEffect, useRef, useState } from "react";
import {
  startPairing,
  submitPairCode,
  type ClaudeAuthStatus,
} from "@/lib/claude-pairing";

interface State {
  phase: "idle" | "starting" | "awaiting_url" | "awaiting_code" | "submitting" | "done" | "error";
  url: string | null;
  error: string | null;
}

const URL_RE = /(https:\/\/claude\.ai\/[^\s]+)/;

export function useClaudePairing(token: string | null) {
  const [state, setState] = useState<State>({ phase: "idle", url: null, error: null });
  const [pairingId, setPairingId] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const begin = useCallback(async () => {
    setState({ phase: "starting", url: null, error: null });
    try {
      const { pairing_id } = await startPairing();
      setPairingId(pairing_id);
      setState({ phase: "awaiting_url", url: null, error: null });
      const ws = new WebSocket(
        `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}` +
          `/ws/claude/pair/${pairing_id}` +
          (token ? `?token=${encodeURIComponent(token)}` : ""),
      );
      wsRef.current = ws;
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === "line") {
          const m = msg.text.match(URL_RE);
          if (m) setState((s) => ({ ...s, phase: "awaiting_code", url: m[1] }));
        }
      };
      ws.onerror = () => setState((s) => ({ ...s, phase: "error", error: "WebSocket error" }));
    } catch (e: any) {
      setState({ phase: "error", url: null, error: e.message });
    }
  }, [token]);

  const submit = useCallback(
    async (code: string) => {
      if (!pairingId) return;
      setState((s) => ({ ...s, phase: "submitting" }));
      try {
        await submitPairCode(pairingId, code);
        setState({ phase: "done", url: null, error: null });
      } catch (e: any) {
        setState({ phase: "error", url: null, error: e.message });
      }
    },
    [pairingId],
  );

  useEffect(() => () => wsRef.current?.close(), []);

  return { state, begin, submit };
}
```

- [ ] **Step 3: Page component**

Create `web-next/app/(app)/settings/claude/page.tsx`:

```typescript
import ConnectClaude from "@/components/settings/connect-claude";

export default function ClaudeSettingsPage() {
  return (
    <div className="p-6 max-w-2xl">
      <h1 className="text-2xl font-semibold mb-4">Connect your Claude account</h1>
      <p className="text-sm text-muted-foreground mb-6">
        Auto-agent runs your tasks under your own Claude subscription. You'll need
        to authenticate once. Tokens stay on the server in a per-user vault and
        are never sent back to your browser.
      </p>
      <ConnectClaude />
    </div>
  );
}
```

- [ ] **Step 3a: Locate the existing JWT-token accessor**

The pairing WebSocket needs the user's JWT in a `?token=` query param (matching the existing `/ws` endpoint pattern in `run.py:190`). The frontend already obtains this token somewhere — find it before writing the component.

Run: `grep -rn "token\|jwt\|/auth/me" web-next/hooks web-next/lib web-next/app | head -30`
Expected: an existing hook like `useAuth`, `useMe`, or a context that holds the token. Reuse that exact mechanism; export a `useAuthToken()` hook from wherever it lives (or alias the existing one). The component below imports it as `useAuthToken`. Do not invent a new auth source.

- [ ] **Step 4: ConnectClaude component**

Create `web-next/components/settings/connect-claude.tsx`:

```typescript
"use client";

import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useClaudePairing } from "@/hooks/useClaudePairing";
import { useAuthToken } from "@/hooks/useAuthToken"; // adjust import path to match step 3a
import { getPairStatus, disconnectClaude } from "@/lib/claude-pairing";

export default function ConnectClaude() {
  const qc = useQueryClient();
  const { data: status } = useQuery({
    queryKey: ["claude-pair-status"],
    queryFn: getPairStatus,
    refetchInterval: 5000,
  });

  const token = useAuthToken(); // see step 3a below
  const { state, begin, submit } = useClaudePairing(token);
  const [code, setCode] = useState("");

  const disconnect = useMutation({
    mutationFn: disconnectClaude,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["claude-pair-status"] }),
  });

  useEffect(() => {
    if (state.phase === "done") {
      qc.invalidateQueries({ queryKey: ["claude-pair-status"] });
    }
  }, [state.phase, qc]);

  if (status?.claude_auth_status === "paired") {
    return (
      <div className="space-y-4">
        <div className="text-green-700">
          Connected{status.claude_paired_at ? ` (since ${new Date(status.claude_paired_at).toLocaleString()})` : ""}.
        </div>
        <Button variant="outline" onClick={() => disconnect.mutate()}>
          Disconnect
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {status?.claude_auth_status === "expired" && (
        <div className="text-amber-700 text-sm">
          Your previous Claude session expired. Reconnect to resume queued tasks.
        </div>
      )}

      {state.phase === "idle" && <Button onClick={begin}>Connect Claude</Button>}

      {state.phase === "starting" && <div>Starting pairing session…</div>}
      {state.phase === "awaiting_url" && <div>Waiting for login URL…</div>}

      {state.phase === "awaiting_code" && state.url && (
        <div className="space-y-3">
          <p className="text-sm">Open this link in a new tab and complete the login:</p>
          <a
            href={state.url}
            target="_blank"
            rel="noreferrer"
            className="text-blue-600 underline break-all"
          >
            {state.url}
          </a>
          <p className="text-sm">Then paste the one-time code below:</p>
          <Input value={code} onChange={(e) => setCode(e.target.value)} placeholder="paste code" />
          <Button disabled={!code} onClick={() => submit(code)}>
            Submit
          </Button>
        </div>
      )}

      {state.phase === "submitting" && <div>Verifying…</div>}
      {state.phase === "done" && <div className="text-green-700">Connected.</div>}
      {state.phase === "error" && (
        <div className="text-red-700 text-sm">{state.error}</div>
      )}
    </div>
  );
}
```

> **Implementer note:** the `token` source in step 4 depends on how the existing app passes the JWT to the WebSocket. Search `web-next/` for the existing main `/ws` connection and reuse that exact pattern (it likely reads from a cookie via document.cookie or from the response of `/auth/me`). Don't invent a new auth mechanism — match what's already there.

- [ ] **Step 5: Manual test**

1. `cd web-next && npm run dev` (in addition to the FastAPI process).
2. Log in to the app.
3. Navigate to `/settings/claude`.
4. Click "Connect Claude". Confirm the URL appears.
5. Open it in a new tab, log in to Claude, get the code.
6. Paste it back, submit.
7. Confirm the page flips to "Connected".
8. Open `/auth/me` in another tab and confirm `claude_auth_status: "paired"`.

- [ ] **Step 6: Commit**

```bash
git add web-next/app/(app)/settings/claude/ web-next/components/settings/ web-next/hooks/useClaudePairing.ts web-next/lib/claude-pairing.ts
git commit -m "feat(web-next): Connect Claude settings page"
```

---

## Task 15: Global "auth expired" banner

**Files:**
- Modify: an existing top-level layout component in `web-next` that already loads `/auth/me` (search for `useQuery` calls keyed `auth-me` or similar).

- [ ] **Step 1: Find the auth-me query**

Run: `grep -rn "auth/me\|auth-me\|useMe\b" web-next/ | head -20`
Expected: a hook or layout component that already fetches the user. Edit there.

- [ ] **Step 2: Render a banner when status != 'paired'**

In that component, add a sticky banner just above the main content:

```tsx
{me?.claude_auth_status && me.claude_auth_status !== "paired" && (
  <div className="bg-amber-100 border-b border-amber-300 text-amber-900 px-4 py-2 text-sm flex items-center justify-between">
    <span>
      {me.claude_auth_status === "expired"
        ? "Your Claude session expired. Reconnect to resume queued tasks."
        : "Connect your Claude account to start queuing tasks."}
    </span>
    <a href="/settings/claude" className="underline font-medium">Connect</a>
  </div>
)}
```

- [ ] **Step 3: Manual test**

1. With a paired user: confirm no banner shows.
2. Manually flip the DB: `UPDATE users SET claude_auth_status='expired' WHERE id=...;`
3. Reload the page — banner appears.
4. Click "Connect", complete pairing — banner disappears within 5s (poll interval).

- [ ] **Step 4: Commit**

```bash
git add web-next/
git commit -m "feat(web-next): global banner for unpaired/expired Claude auth"
```

---

## Task 16: End-to-end verification

- [ ] **Step 1: Run all unit tests**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 2: Lint**

Run: `ruff check . && ruff format --check .`
Expected: no errors.

- [ ] **Step 3: Manual end-to-end on the VM**

1. Deploy the branch to the VM.
2. As user A: log in, go to Settings → Connect Claude, complete the pairing flow.
3. As user B (different login): same.
4. Queue a task as user A. Confirm via logs that the worker spawned with `HOME=/data/users/<A.id>`.
5. While A's task is running, queue a task as B on a different repo. Confirm both run concurrently.
6. Queue a third task as A on the same repo as A's first task — confirm it sits in `QUEUED`.
7. Queue 3 more tasks across various users/repos — confirm 5 run concurrently, additional ones queue.
8. On the VM, manually corrupt user A's vault (`rm /data/users/<A.id>/.claude/.credentials.json`). Queue a new task as A — confirm it transitions to `BLOCKED_ON_AUTH` and the banner appears for A.
9. Re-pair as A — confirm the blocked task auto-requeues to `QUEUED`.

- [ ] **Step 4: Commit any final fixes**

If anything was missed, fix and commit before declaring done.

---

## Operational notes (not tasks)

- **Rollout sequence:** apply migration first (Task 2), deploy backend changes (Tasks 3–13), then frontend (Tasks 14–15). Existing logged-in users see `claude_auth_status='never_paired'` and the banner; their unfinished tasks are unaffected (existing tasks have `created_by_user_id` populated and will be probed at the next dispatch).
- **Existing in-flight tasks:** if the migration runs while tasks are mid-flight, those tasks finish under the legacy single-credential setup (the dispatch-time probe only runs at task start). New tasks will probe and route correctly.
- **Disk:** confirm `/data` is on the encrypted persistent disk on the VM before deploy. `df /data && lsblk -f` should show the encrypted volume mounted there.
- **Bedrock/native-Anthropic providers:** untouched. Tasks routed to those providers continue to use the shared AWS/API credentials in env. Only the `claude_cli` provider becomes per-user.
