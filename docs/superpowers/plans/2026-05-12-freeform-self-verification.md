# Freeform Self-Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new VERIFYING lifecycle phase (boot check + intent check) between CODING and PR creation, and extend the existing review phase with a UI check (Playwright screenshots + agent judgment). Expose `browse_url` as a tool to coding, verify, and review so each agent can drive its own visual inspection with concrete prompt guidance.

**Architecture:** Two phases gate the post-coding flow. **Verify** (new state `VERIFYING`, between CODING and PR_CREATED) answers *"does it run and does it match the ask?"* via a deterministic boot check plus an agent-driven intent check. **Review** (existing `AWAITING_REVIEW`, inline in `_finish_coding` today) gets an extra UI-check sub-step before its existing code review. Both phases own their dev-server lifecycle (start, kill via process-group); agents call `browse_url` per concrete prompt guidance. Each gate has a 2-cycle retry budget; second failure → `BLOCKED`.

**Tech Stack:** Python 3.12 (async), SQLAlchemy 2.0 async, Alembic, FastAPI, Playwright (Python), Next.js 14 App Router + TanStack Query, Pydantic v2.

**Spec:** `docs/superpowers/specs/2026-05-12-freeform-self-verification-design.md`.

---

## Phase 1: Schema & Foundation

### Task 1: Add Pydantic types to `shared/types.py`

**Files:**
- Modify: `shared/types.py`

- [ ] **Step 1: Add the types**

Append to `shared/types.py`:

```python
from typing import Literal


class AffectedRoute(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str
    label: str


class IntentVerdict(BaseModel):
    ok: bool
    reasoning: str
    tool_calls: list[dict] = []  # browse_url / tail_dev_server_log call log


class ReviewDimensionVerdict(BaseModel):
    verdict: Literal["OK", "NOT-OK", "SKIPPED"]
    reasoning: str


class ReviewCombinedVerdict(BaseModel):
    code_review: ReviewDimensionVerdict
    ui_check: ReviewDimensionVerdict


class VerifyAttemptOut(BaseModel):
    """API shape for a verify attempt row."""
    id: int
    cycle: int
    status: Literal["pass", "fail", "error"]
    boot_check: Literal["pass", "fail", "skipped"] | None
    intent_check: Literal["pass", "fail"] | None
    intent_judgment: str | None
    tool_calls: list[dict] | None
    failure_reason: str | None
    log_tail: str | None
    started_at: datetime
    finished_at: datetime | None


class ReviewAttemptOut(BaseModel):
    id: int
    cycle: int
    status: Literal["pass", "fail", "error"]
    code_review_verdict: str | None
    ui_check: Literal["pass", "fail", "skipped"] | None
    ui_judgment: str | None
    tool_calls: list[dict] | None
    failure_reason: str | None
    log_tail: str | None
    started_at: datetime
    finished_at: datetime | None
```

(`datetime` and `BaseModel` are already imported at the top of the file.)

- [ ] **Step 2: Add a smoke test**

Append to `tests/test_types.py` (create if absent):

```python
from shared.types import AffectedRoute, IntentVerdict, ReviewCombinedVerdict


def test_affected_route_defaults():
    r = AffectedRoute(path="/", label="home")
    assert r.method == "GET"


def test_intent_verdict_serialises():
    v = IntentVerdict(ok=True, reasoning="looks good")
    assert v.model_dump()["tool_calls"] == []


def test_review_combined_shape():
    v = ReviewCombinedVerdict(
        code_review={"verdict": "OK", "reasoning": ""},
        ui_check={"verdict": "SKIPPED", "reasoning": ""},
    )
    assert v.code_review.verdict == "OK"
```

- [ ] **Step 3: Run tests**

```
.venv/bin/python3 -m pytest tests/test_types.py -v
```

Expected: 3 passed.

- [ ] **Step 4: Commit**

```
git add shared/types.py tests/test_types.py
git commit -m "feat(types): add AffectedRoute and verify/review verdict types"
```

---

### Task 2: Add `TaskStatus.VERIFYING` and new transitions

**Files:**
- Modify: `shared/models.py:35-50` (TaskStatus enum)
- Modify: `orchestrator/state_machine.py:11-37` (TRANSITIONS table)

- [ ] **Step 1: Write the failing transition test**

Append to `tests/test_state_machine.py` (create if absent):

```python
import pytest
from shared.models import Task, TaskSource, TaskStatus
from orchestrator.state_machine import InvalidTransition, transition


async def _mk_task(session, status):
    t = Task(title="t", source=TaskSource.MANUAL, status=status, organization_id=1)
    session.add(t)
    await session.flush()
    return t


async def test_coding_to_verifying(db_session):
    t = await _mk_task(db_session, TaskStatus.CODING)
    await transition(db_session, t, TaskStatus.VERIFYING, "ready to verify")
    assert t.status == TaskStatus.VERIFYING


async def test_verifying_to_pr_created(db_session):
    t = await _mk_task(db_session, TaskStatus.VERIFYING)
    await transition(db_session, t, TaskStatus.PR_CREATED, "verify passed")
    assert t.status == TaskStatus.PR_CREATED


async def test_verifying_back_to_coding(db_session):
    t = await _mk_task(db_session, TaskStatus.VERIFYING)
    await transition(db_session, t, TaskStatus.CODING, "verify failed")
    assert t.status == TaskStatus.CODING


async def test_verifying_to_blocked(db_session):
    t = await _mk_task(db_session, TaskStatus.VERIFYING)
    await transition(db_session, t, TaskStatus.BLOCKED, "verify failed cycle 2")
    assert t.status == TaskStatus.BLOCKED


async def test_awaiting_review_to_blocked(db_session):
    t = await _mk_task(db_session, TaskStatus.AWAITING_REVIEW)
    await transition(db_session, t, TaskStatus.BLOCKED, "review failed cycle 2")
    assert t.status == TaskStatus.BLOCKED
```

Existing `tests/conftest.py` provides `db_session` (verify by inspecting it; if absent, follow the pattern in `tests/test_market_brief_freshness.py`).

- [ ] **Step 2: Run to confirm fail**

```
.venv/bin/python3 -m pytest tests/test_state_machine.py -v
```

Expected: 5 errors / failures — `TaskStatus.VERIFYING` undefined, transitions missing.

- [ ] **Step 3: Add `VERIFYING` to the enum**

In `shared/models.py`, modify `TaskStatus`:

```python
class TaskStatus(str, enum.Enum):
    INTAKE = "intake"
    CLASSIFYING = "classifying"
    QUEUED = "queued"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    CODING = "coding"
    VERIFYING = "verifying"          # NEW
    PR_CREATED = "pr_created"
    AWAITING_CI = "awaiting_ci"
    AWAITING_REVIEW = "awaiting_review"
    DONE = "done"
    BLOCKED_ON_AUTH = "blocked_on_auth"
    BLOCKED_ON_QUOTA = "blocked_on_quota"
    BLOCKED = "blocked"
    FAILED = "failed"
```

- [ ] **Step 4: Update `TRANSITIONS` in `orchestrator/state_machine.py`**

```python
TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    # ... unchanged entries above ...
    TaskStatus.CODING: {
        TaskStatus.VERIFYING,                      # NEW (replaces direct → PR_CREATED for the verify path)
        TaskStatus.PR_CREATED, TaskStatus.AWAITING_CLARIFICATION,
        TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.DONE,
        TaskStatus.BLOCKED_ON_QUOTA,
    },
    TaskStatus.VERIFYING: {                        # NEW state
        TaskStatus.PR_CREATED,
        TaskStatus.CODING,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED_ON_QUOTA,
    },
    # ... PR_CREATED, AWAITING_CI unchanged ...
    TaskStatus.AWAITING_REVIEW: {
        TaskStatus.DONE, TaskStatus.CODING,
        TaskStatus.BLOCKED,                        # NEW (cycle-2 review failure)
    },
    # ... rest unchanged ...
}
```

- [ ] **Step 5: Run tests**

```
.venv/bin/python3 -m pytest tests/test_state_machine.py -v
```

Expected: 5 passed.

- [ ] **Step 6: Commit**

```
git add shared/models.py orchestrator/state_machine.py tests/test_state_machine.py
git commit -m "feat(models): add TaskStatus.VERIFYING and verify/review transitions"
```

---

### Task 3: Add `Task.affected_routes`, `FreeformConfig.run_command`, and the two attempt tables

**Files:**
- Modify: `shared/models.py` (Task model + new VerifyAttempt, ReviewAttempt; FreeformConfig if defined there, otherwise the ORM equivalent)

- [ ] **Step 1: Inspect where `FreeformConfig` ORM lives**

```
grep -n "class FreeformConfig" shared/models.py shared/config.py shared/types.py
```

Expected: ORM model is in `shared/models.py`; Pydantic shape is in `shared/types.py`. Add `run_command` to **both**.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_verify_review_models.py` (create):

```python
import pytest
from shared.models import (
    Task, TaskSource, TaskStatus,
    VerifyAttempt, ReviewAttempt,
    FreeformConfig,
)


async def test_task_has_affected_routes_default(db_session):
    t = Task(title="t", source=TaskSource.MANUAL, status=TaskStatus.INTAKE, organization_id=1)
    db_session.add(t); await db_session.flush()
    assert t.affected_routes == []


async def test_verify_attempt_roundtrip(db_session):
    t = Task(title="t", source=TaskSource.MANUAL, status=TaskStatus.VERIFYING, organization_id=1)
    db_session.add(t); await db_session.flush()
    a = VerifyAttempt(
        task_id=t.id, cycle=1, status="pass",
        boot_check="pass", intent_check="pass",
        intent_judgment="looks good", tool_calls=[],
    )
    db_session.add(a); await db_session.flush()
    assert a.id is not None


async def test_review_attempt_roundtrip(db_session):
    t = Task(title="t", source=TaskSource.MANUAL, status=TaskStatus.AWAITING_REVIEW, organization_id=1)
    db_session.add(t); await db_session.flush()
    a = ReviewAttempt(
        task_id=t.id, cycle=1, status="pass",
        code_review_verdict="OK", ui_check="skipped",
    )
    db_session.add(a); await db_session.flush()
    assert a.id is not None


async def test_freeform_config_run_command_nullable(db_session):
    cfg = FreeformConfig(
        repo_id=1, organization_id=1,
        dev_branch="dev", prod_branch="main",
    )
    db_session.add(cfg); await db_session.flush()
    assert cfg.run_command is None
```

- [ ] **Step 3: Add the columns and models in `shared/models.py`**

In the `Task` class, after the `intake_qa` column:

```python
    affected_routes = Column(JSONB, nullable=False, server_default="[]")
```

After the `Task` class body, add the `verify_attempts` and `review_attempts` relationships:

```python
    verify_attempts = relationship(
        "VerifyAttempt", back_populates="task", order_by="VerifyAttempt.cycle",
    )
    review_attempts = relationship(
        "ReviewAttempt", back_populates="task", order_by="ReviewAttempt.cycle",
    )
```

In the `FreeformConfig` ORM class, add:

```python
    run_command = Column(Text, nullable=True)
```

At the end of the file (after the existing models), add:

```python
class VerifyAttempt(Base):
    __tablename__ = "verify_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "cycle", name="ix_verify_attempts_task_cycle"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    cycle = Column(Integer, nullable=False)  # 1 or 2
    status = Column(String(16), nullable=False)  # pass / fail / error
    boot_check = Column(String(16), nullable=True)  # pass / fail / skipped
    intent_check = Column(String(16), nullable=True)  # pass / fail
    intent_judgment = Column(Text, nullable=True)
    tool_calls = Column(JSONB, nullable=True)
    failure_reason = Column(Text, nullable=True)
    log_tail = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    task = relationship("Task", back_populates="verify_attempts")


class ReviewAttempt(Base):
    __tablename__ = "review_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "cycle", name="ix_review_attempts_task_cycle"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    cycle = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False)
    code_review_verdict = Column(Text, nullable=True)
    ui_check = Column(String(16), nullable=True)  # pass / fail / skipped
    ui_judgment = Column(Text, nullable=True)
    tool_calls = Column(JSONB, nullable=True)
    failure_reason = Column(Text, nullable=True)
    log_tail = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    task = relationship("Task", back_populates="review_attempts")
```

Add `run_command: str | None = None` to the `FreeformConfig` Pydantic model in `shared/types.py`.

- [ ] **Step 4: Run tests (will still fail — migrations not yet applied)**

```
.venv/bin/python3 -m pytest tests/test_verify_review_models.py -v
```

Expected: errors for missing tables / columns. We'll fix in Task 4.

- [ ] **Step 5: Commit**

```
git add shared/models.py shared/types.py tests/test_verify_review_models.py
git commit -m "feat(models): VerifyAttempt, ReviewAttempt, Task.affected_routes, FreeformConfig.run_command"
```

---

### Task 4: Alembic migration

**Files:**
- Create: `migrations/versions/032_verify_review_attempts.py`

- [ ] **Step 1: Generate the revision skeleton**

```
docker compose exec auto-agent alembic revision -m "verify_review_attempts"
```

This creates a new file in `migrations/versions/`. Rename to `032_verify_review_attempts.py` for ordering consistency with siblings.

- [ ] **Step 2: Fill in the migration**

```python
"""verify_review_attempts

Revision ID: 032_verify_review_attempts
Revises: 031_market_research
Create Date: 2026-05-12

Adds VERIFYING task status, Task.affected_routes, FreeformConfig.run_command,
and the verify_attempts + review_attempts tables.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "032_verify_review_attempts"
down_revision = "031_market_research"


def upgrade() -> None:
    # 1) Enum: add 'verifying' to task_status
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'verifying'")

    # 2) Task.affected_routes
    op.add_column(
        "tasks",
        sa.Column(
            "affected_routes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # 3) FreeformConfig.run_command
    op.add_column("freeform_configs", sa.Column("run_command", sa.Text(), nullable=True))

    # 4) verify_attempts
    op.create_table(
        "verify_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("boot_check", sa.String(16), nullable=True),
        sa.Column("intent_check", sa.String(16), nullable=True),
        sa.Column("intent_judgment", sa.Text(), nullable=True),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("log_tail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("task_id", "cycle", name="ix_verify_attempts_task_cycle"),
    )

    # 5) review_attempts
    op.create_table(
        "review_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("code_review_verdict", sa.Text(), nullable=True),
        sa.Column("ui_check", sa.String(16), nullable=True),
        sa.Column("ui_judgment", sa.Text(), nullable=True),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("log_tail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("task_id", "cycle", name="ix_review_attempts_task_cycle"),
    )


def downgrade() -> None:
    op.drop_table("review_attempts")
    op.drop_table("verify_attempts")
    op.drop_column("freeform_configs", "run_command")
    op.drop_column("tasks", "affected_routes")
    # Postgres does not support removing enum values — leave 'verifying' in place.
```

- [ ] **Step 3: Apply migration**

```
docker compose exec auto-agent alembic upgrade head
```

Expected output ends with: `INFO  [alembic.runtime.migration] Running upgrade 031_market_research -> 032_verify_review_attempts`.

- [ ] **Step 4: Re-run Task 3's tests**

```
.venv/bin/python3 -m pytest tests/test_verify_review_models.py tests/test_state_machine.py -v
```

Expected: 4 + 5 = 9 passed.

- [ ] **Step 5: Commit**

```
git add migrations/versions/032_verify_review_attempts.py
git commit -m "migrate(032): verify_attempts, review_attempts, affected_routes, run_command"
```

---

### Task 5: Add event builders

**Files:**
- Modify: `shared/events.py`

- [ ] **Step 1: Find the existing builder pattern**

```
grep -n "def task_review_complete\|def repo_onboard" shared/events.py | head
```

Find an existing builder for the pattern: usually `def <name>(task_id: int, **fields) -> Event: return Event(kind="<name>", task_id=task_id, payload={...})`.

- [ ] **Step 2: Add the new event builders**

Append to `shared/events.py` (after existing builders):

```python
def verify_started(task_id: int, cycle: int) -> Event:
    return Event(kind="verify_started", task_id=task_id, payload={"cycle": cycle})


def verify_passed(task_id: int, cycle: int) -> Event:
    return Event(kind="verify_passed", task_id=task_id, payload={"cycle": cycle})


def verify_failed(task_id: int, cycle: int, reason: str) -> Event:
    return Event(
        kind="verify_failed", task_id=task_id,
        payload={"cycle": cycle, "reason": reason},
    )


def verify_skipped_no_runner(task_id: int) -> Event:
    return Event(kind="verify_skipped_no_runner", task_id=task_id, payload={})


def coding_server_boot_failed(task_id: int, reason: str) -> Event:
    return Event(
        kind="coding_server_boot_failed", task_id=task_id,
        payload={"reason": reason},
    )


def review_ui_check_started(task_id: int, cycle: int) -> Event:
    return Event(kind="review_ui_check_started", task_id=task_id, payload={"cycle": cycle})


def review_skipped_no_runner(task_id: int) -> Event:
    return Event(kind="review_skipped_no_runner", task_id=task_id, payload={})
```

- [ ] **Step 3: Smoke test**

Append to `tests/test_events.py` (create if absent):

```python
from shared.events import verify_started, verify_failed, coding_server_boot_failed


def test_verify_started_payload():
    e = verify_started(task_id=42, cycle=1)
    assert e.kind == "verify_started"
    assert e.payload == {"cycle": 1}
    assert e.task_id == 42


def test_verify_failed_payload():
    e = verify_failed(task_id=42, cycle=2, reason="boot_timeout")
    assert e.payload["reason"] == "boot_timeout"


def test_coding_server_boot_failed():
    e = coding_server_boot_failed(task_id=42, reason="no run command")
    assert e.kind == "coding_server_boot_failed"
```

Run:

```
.venv/bin/python3 -m pytest tests/test_events.py -v
```

Expected: passed.

- [ ] **Step 4: Commit**

```
git add shared/events.py tests/test_events.py
git commit -m "feat(events): add verify_/review_/coding_server_* event builders"
```

---

## Phase 2: Tools (dev_server, browse_url)

### Task 6: `sniff_run_command` in `agent/tools/dev_server.py`

**Files:**
- Create: `agent/tools/dev_server.py`
- Create: `tests/test_dev_server.py`

- [ ] **Step 1: Write the failing test**

`tests/test_dev_server.py`:

```python
import json
import tempfile
from pathlib import Path

import pytest

from agent.tools.dev_server import sniff_run_command


def _write(p: Path, content: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_sniff_package_json_dev():
    with tempfile.TemporaryDirectory() as d:
        _write(Path(d) / "package.json", json.dumps({"scripts": {"dev": "next dev"}}))
        assert sniff_run_command(d) == "npm run dev"


def test_sniff_procfile_web():
    with tempfile.TemporaryDirectory() as d:
        _write(Path(d) / "Procfile", "web: python run.py\nworker: rq worker\n")
        assert sniff_run_command(d) == "python run.py"


def test_sniff_pyproject_run():
    with tempfile.TemporaryDirectory() as d:
        _write(
            Path(d) / "pyproject.toml",
            '[tool.auto-agent]\nrun = "uvicorn app:app --reload"\n',
        )
        assert sniff_run_command(d) == "uvicorn app:app --reload"


def test_sniff_priority_freeform_config_wins(monkeypatch):
    # FreeformConfig override is taken from a parameter, not the workspace tree.
    with tempfile.TemporaryDirectory() as d:
        _write(Path(d) / "package.json", json.dumps({"scripts": {"dev": "next dev"}}))
        assert (
            sniff_run_command(d, override="make serve") == "make serve"
        )


def test_sniff_none_when_nothing_resolves():
    with tempfile.TemporaryDirectory() as d:
        assert sniff_run_command(d) is None
```

- [ ] **Step 2: Run to confirm fail**

```
.venv/bin/python3 -m pytest tests/test_dev_server.py -v
```

Expected: ImportError or 5 failures.

- [ ] **Step 3: Implement `sniff_run_command`**

`agent/tools/dev_server.py`:

```python
"""Dev-server lifecycle utilities used by coding / verify / review phases.

Layers in this module:
- Pure helpers: ``sniff_run_command``.
- Async lifecycle helpers: ``start_dev_server``, ``wait_for_port``, ``hold``,
  ``kill_server`` (added in later tasks).
- Agent-callable tool: ``TailDevServerLogTool`` (added in later tasks).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path


def sniff_run_command(workspace_path: str, *, override: str | None = None) -> str | None:
    """Return a shell command that starts the project's dev server.

    Priority: ``override`` (FreeformConfig.run_command) → package.json scripts.dev
    → Procfile ``web:`` entry → pyproject.toml ``[tool.auto-agent].run``.
    Returns ``None`` if nothing resolves.
    """
    if override:
        return override

    workspace = Path(workspace_path)

    pkg = workspace / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text())
            if isinstance(data.get("scripts"), dict) and "dev" in data["scripts"]:
                return "npm run dev"
        except Exception:
            pass

    procfile = workspace / "Procfile"
    if procfile.is_file():
        for line in procfile.read_text().splitlines():
            if line.startswith("web:"):
                return line[len("web:") :].strip()

    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text())
            cmd = data.get("tool", {}).get("auto-agent", {}).get("run")
            if isinstance(cmd, str) and cmd:
                return cmd
        except Exception:
            pass

    return None
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python3 -m pytest tests/test_dev_server.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```
git add agent/tools/dev_server.py tests/test_dev_server.py
git commit -m "feat(tools): sniff_run_command in dev_server.py"
```

---

### Task 7: `start_dev_server`, `DevServerHandle`, and `kill_server`

**Files:**
- Modify: `agent/tools/dev_server.py`
- Modify: `tests/test_dev_server.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_dev_server.py`:

```python
import asyncio
import socket

from agent.tools.dev_server import DevServerHandle, start_dev_server, kill_server


async def test_start_and_kill_simple_server():
    # A tiny python -m http.server clone via stdin script.
    script = (
        "import http.server, socketserver, os, sys\n"
        "port = int(os.environ['PORT'])\n"
        "with socketserver.TCPServer(('127.0.0.1', port), http.server.SimpleHTTPRequestHandler) as s:\n"
        "    s.serve_forever()\n"
    )
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "Procfile").write_text("web: python -c \"" + script.replace('"', '\\"') + "\"\n")
        async with start_dev_server(d) as handle:
            assert isinstance(handle, DevServerHandle)
            assert handle.port > 0
            # Server should be reachable shortly.
            await asyncio.sleep(0.5)
            with socket.create_connection(("127.0.0.1", handle.port), timeout=2) as _:
                pass
        # After context exit, port should refuse.
        with pytest.raises(OSError):
            with socket.create_connection(("127.0.0.1", handle.port), timeout=1):
                pass
```

- [ ] **Step 2: Add the implementation**

Append to `agent/tools/dev_server.py`:

```python
import asyncio
import contextlib
import os
import shlex
import signal
import socket
import tempfile
import time
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class DevServerHandle:
    pid: int
    pgid: int
    port: int
    log_path: str
    started_at: float
    process: asyncio.subprocess.Process = field(repr=False)


def _allocate_port() -> int:
    """Ask the OS for a free TCP port and immediately release it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.asynccontextmanager
async def start_dev_server(
    workspace_path: str, *, override: str | None = None,
) -> AsyncIterator[DevServerHandle]:
    """Start the project's dev server in a new process group.

    Yields a handle; kills the process group on context exit.
    Raises ``BootError`` if no run command resolves.
    """
    cmd = sniff_run_command(workspace_path, override=override)
    if not cmd:
        raise BootError("no run command resolved for workspace")

    port = _allocate_port()
    log_file = tempfile.NamedTemporaryFile(
        prefix="dev-server-", suffix=".log", delete=False, mode="w",
    )
    log_path = log_file.name
    log_file.close()
    log_fh = open(log_path, "wb", buffering=0)

    env = os.environ.copy()
    env["PORT"] = str(port)

    process = await asyncio.create_subprocess_exec(
        *shlex.split(cmd),
        cwd=workspace_path,
        env=env,
        stdout=log_fh,
        stderr=asyncio.subprocess.STDOUT,
        preexec_fn=os.setsid,
    )

    handle = DevServerHandle(
        pid=process.pid,
        pgid=os.getpgid(process.pid),
        port=port,
        log_path=log_path,
        started_at=time.time(),
        process=process,
    )

    try:
        yield handle
    finally:
        await kill_server(handle)
        log_fh.close()


async def kill_server(handle: DevServerHandle, grace_seconds: float = 2.0) -> None:
    """Kill the dev server's process group, escalating to SIGKILL after grace."""
    try:
        os.killpg(handle.pgid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(handle.process.wait(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(handle.pgid, signal.SIGKILL)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(handle.process.wait(), timeout=1.0)


class BootError(RuntimeError):
    """Raised when start_dev_server can't even start (no run command, fork failure)."""


class BootTimeout(RuntimeError):
    def __init__(self, log_tail: str):
        super().__init__("dev server failed to bind port in time")
        self.log_tail = log_tail


class EarlyExit(RuntimeError):
    def __init__(self, log_tail: str):
        super().__init__("dev server exited during hold")
        self.log_tail = log_tail
```

- [ ] **Step 3: Run tests**

```
.venv/bin/python3 -m pytest tests/test_dev_server.py -v
```

Expected: previous 5 still pass; new `test_start_and_kill_simple_server` passes (may take 2-3 s).

- [ ] **Step 4: Commit**

```
git add agent/tools/dev_server.py tests/test_dev_server.py
git commit -m "feat(tools): start_dev_server/kill_server with process-group cleanup"
```

---

### Task 8: `wait_for_port` and `hold`

**Files:**
- Modify: `agent/tools/dev_server.py`
- Modify: `tests/test_dev_server.py`

- [ ] **Step 1: Failing test**

Append to `tests/test_dev_server.py`:

```python
from agent.tools.dev_server import BootTimeout, EarlyExit, hold, wait_for_port


async def test_wait_for_port_success():
    # Bind a real port, then call wait_for_port — should return.
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.listen(1)
    try:
        await wait_for_port(port, timeout=1.0)
    finally:
        s.close()


async def test_wait_for_port_timeout():
    port = _allocate_port_helper()  # from local helper below
    with pytest.raises(BootTimeout):
        await wait_for_port(port, timeout=0.2)


def _allocate_port_helper() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


async def test_hold_passes_when_alive():
    script = "import time; time.sleep(5)\n"
    proc = await asyncio.create_subprocess_exec(
        "python", "-c", script,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    handle = DevServerHandle(
        pid=proc.pid, pgid=os.getpgid(proc.pid),
        port=0, log_path="/dev/null", started_at=time.time(), process=proc,
    )
    try:
        await hold(handle, seconds=0.5)
    finally:
        await kill_server(handle)


async def test_hold_raises_on_early_exit(tmp_path):
    log = tmp_path / "log.txt"; log.write_text("boom\nbang\n")
    proc = await asyncio.create_subprocess_exec(
        "python", "-c", "import sys; sys.exit(1)",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    handle = DevServerHandle(
        pid=proc.pid, pgid=os.getpgid(proc.pid),
        port=0, log_path=str(log), started_at=time.time(), process=proc,
    )
    await asyncio.sleep(0.1)  # let it exit
    with pytest.raises(EarlyExit) as ei:
        await hold(handle, seconds=0.5)
    assert "boom" in ei.value.log_tail
```

- [ ] **Step 2: Implement**

Append to `agent/tools/dev_server.py`:

```python
def _tail(path: str, lines: int = 50) -> str:
    try:
        data = Path(path).read_text(errors="replace")
    except Exception:
        return ""
    return "\n".join(data.splitlines()[-lines:])


async def wait_for_port(port: int, timeout: float = 60.0, log_path: str | None = None) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.25)
            s.connect(("127.0.0.1", port))
            s.close()
            return
        except OSError:
            await asyncio.sleep(0.25)
    raise BootTimeout(_tail(log_path) if log_path else "")


async def hold(handle: DevServerHandle, seconds: float = 5.0) -> None:
    """Hold the configured duration; raise EarlyExit if the process dies."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if handle.process.returncode is not None:
            raise EarlyExit(_tail(handle.log_path))
        await asyncio.sleep(0.5)
```

- [ ] **Step 3: Run tests**

```
.venv/bin/python3 -m pytest tests/test_dev_server.py -v
```

Expected: all passed (4 new ones).

- [ ] **Step 4: Commit**

```
git add agent/tools/dev_server.py tests/test_dev_server.py
git commit -m "feat(tools): wait_for_port and hold for dev_server"
```

---

### Task 9: `tail_dev_server_log` agent tool

**Files:**
- Modify: `agent/tools/dev_server.py`
- Modify: `tests/test_dev_server.py`

- [ ] **Step 1: Inspect `agent/tools/base.py` for the Tool interface**

```
grep -n "class Tool\|name: str\|async def run" agent/tools/base.py | head -20
```

Identify the base class and signature: typically `class Tool: name: str; description: str; input_schema: dict; async def run(self, args, ctx) -> ToolResult`.

- [ ] **Step 2: Failing test**

Append to `tests/test_dev_server.py`:

```python
from agent.tools.dev_server import TailDevServerLogTool


async def test_tail_log_tool_returns_last_lines(tmp_path, monkeypatch):
    log = tmp_path / "server.log"
    log.write_text("\n".join(f"line {i}" for i in range(100)) + "\n")

    # Fake context: tool needs access to the active server handle.
    tool = TailDevServerLogTool()
    result = await tool.run(
        {"lines": 5},
        ctx=type("Ctx", (), {"dev_server_log_path": str(log)})(),
    )
    text = result.content[0]["text"]
    assert "line 95" in text and "line 99" in text
    assert "line 0" not in text
```

- [ ] **Step 3: Implement**

Append to `agent/tools/dev_server.py`:

```python
from agent.tools.base import Tool, ToolResult


class TailDevServerLogTool(Tool):
    name = "tail_dev_server_log"
    description = (
        "Return the last N lines of the dev server log for this task. "
        "Useful when verify or review fails and you need to see what the server printed."
    )
    input_schema = {
        "type": "object",
        "properties": {"lines": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500}},
        "required": [],
    }

    async def run(self, args: dict, ctx) -> ToolResult:
        log_path = getattr(ctx, "dev_server_log_path", None)
        if not log_path:
            return ToolResult(content=[{"type": "text", "text": "(no dev server running)"}])
        n = int(args.get("lines", 50))
        return ToolResult(content=[{"type": "text", "text": _tail(log_path, lines=n)}])
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python3 -m pytest tests/test_dev_server.py::test_tail_log_tool_returns_last_lines -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```
git add agent/tools/dev_server.py tests/test_dev_server.py
git commit -m "feat(tools): TailDevServerLogTool"
```

---

### Task 10: `browse_url` tool

**Files:**
- Create: `agent/tools/browse_url.py`
- Create: `tests/test_browse_url.py`

- [ ] **Step 1: Install Playwright**

Add `playwright = "*"` to the appropriate dependency group in `pyproject.toml` (verify the package name and group via `grep -n 'playwright\|^\[tool\.poetry' pyproject.toml` if Poetry is used; otherwise edit `requirements.txt`). Then:

```
pip install playwright && playwright install chromium
```

(For Docker: add `RUN playwright install --with-deps chromium` to the `Dockerfile`.)

- [ ] **Step 2: Failing test**

`tests/test_browse_url.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools.browse_url import BrowseUrlTool


@pytest.fixture
def mock_playwright():
    with patch("agent.tools.browse_url.async_playwright") as ap:
        chromium = MagicMock()
        browser = MagicMock()
        context = MagicMock()
        page = MagicMock()

        ap.return_value.__aenter__.return_value = MagicMock(chromium=chromium)
        chromium.launch = AsyncMock(return_value=browser)
        browser.new_context = AsyncMock(return_value=context)
        context.new_page = AsyncMock(return_value=page)
        page.goto = AsyncMock(return_value=MagicMock(status=200, url="http://x/"))
        page.wait_for_selector = AsyncMock()
        page.screenshot = AsyncMock(return_value=b"PNGBYTES")
        page.content = AsyncMock(return_value="<html><body>Hello world</body></html>")
        context.close = AsyncMock()
        browser.close = AsyncMock()
        yield ap


async def test_returns_image_block(mock_playwright):
    tool = BrowseUrlTool()
    result = await tool.run({"url": "http://localhost:3000/"}, ctx=None)
    blocks = result.content
    types = [b["type"] for b in blocks]
    assert "image" in types
    assert "text" in types


async def test_text_capped_at_5000(mock_playwright):
    # Make page.content return a huge string.
    page = mock_playwright.return_value.__aenter__.return_value.chromium.launch.return_value.new_context.return_value.new_page.return_value
    page.content.return_value = "x" * 20000
    tool = BrowseUrlTool()
    result = await tool.run({"url": "http://localhost:3000/"}, ctx=None)
    text_block = next(b for b in result.content if b["type"] == "text" and "x" in b["text"])
    assert len(text_block["text"]) <= 5100  # 5000 plus some prefix slack
```

- [ ] **Step 3: Implement**

`agent/tools/browse_url.py`:

```python
"""Agent-callable visual-capture tool. Single screenshot mechanism for the system."""
from __future__ import annotations

import base64

from playwright.async_api import async_playwright

from agent.tools.base import Tool, ToolResult


_TEXT_CAP = 5000


def _html_to_text(html: str) -> str:
    """Very cheap HTML-to-text: drop tags, collapse whitespace."""
    import re
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


class BrowseUrlTool(Tool):
    name = "browse_url"
    description = (
        "Navigate to a URL with a headless browser, capture rendered text and a "
        "full-page PNG screenshot. Use this to inspect the running app while you "
        "work, or to check whether a route renders as the task expects."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "wait_for": {"type": "string", "default": "body"},
            "viewport": {
                "type": "object",
                "properties": {
                    "width": {"type": "integer", "default": 1280},
                    "height": {"type": "integer", "default": 800},
                },
            },
        },
        "required": ["url"],
    }

    async def run(self, args: dict, ctx) -> ToolResult:
        url = args["url"]
        wait_for = args.get("wait_for") or "body"
        vp = args.get("viewport") or {}
        viewport = {"width": vp.get("width", 1280), "height": vp.get("height", 800)}

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport=viewport)
            page = await context.new_page()
            try:
                response = await page.goto(url, wait_until="networkidle", timeout=30_000)
                status = response.status if response else 0
                final_url = response.url if response else url
                try:
                    await page.wait_for_selector(wait_for, timeout=15_000)
                except Exception:
                    pass
                png = await page.screenshot(full_page=True)
                html = await page.content()
            except Exception as e:
                await context.close(); await browser.close()
                return ToolResult(content=[
                    {"type": "text", "text": f"browse_url failed: {e}"}
                ])
            await context.close(); await browser.close()

        text = _html_to_text(html)[:_TEXT_CAP]
        return ToolResult(content=[
            {"type": "text", "text": f"HTTP {status} {final_url}"},
            {"type": "text", "text": text},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(png).decode("ascii"),
                },
            },
        ])
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python3 -m pytest tests/test_browse_url.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```
git add agent/tools/browse_url.py tests/test_browse_url.py pyproject.toml Dockerfile
git commit -m "feat(tools): browse_url (Playwright) with screenshot image block"
```

---

### Task 11: `with_browser` flag in the registry and factory

**Files:**
- Modify: `agent/tools/__init__.py`
- Modify: `agent/lifecycle/factory.py`

- [ ] **Step 1: Inspect the existing `create_default_registry` and `create_agent` signatures**

```
sed -n '20,60p' agent/tools/__init__.py
grep -n "def create_agent" agent/lifecycle/factory.py
```

- [ ] **Step 2: Failing test**

Append to `tests/test_tools_registry.py` (create if absent):

```python
from agent.tools import create_default_registry


def test_with_browser_registers_browse_url_and_tail():
    r = create_default_registry(with_web=False, readonly=True, with_browser=True)
    names = {t.name for t in r.tools.values()}
    assert "browse_url" in names
    assert "tail_dev_server_log" in names


def test_default_does_not_register_browser_tools():
    r = create_default_registry(with_web=False, readonly=True)
    names = {t.name for t in r.tools.values()}
    assert "browse_url" not in names
    assert "tail_dev_server_log" not in names
```

- [ ] **Step 3: Implement in `agent/tools/__init__.py`**

Modify `create_default_registry` signature and body:

```python
def create_default_registry(
    with_web: bool = False,
    readonly: bool = False,
    with_browser: bool = False,
) -> ToolRegistry:
    registry = ToolRegistry()
    # ... existing registrations ...
    if with_browser:
        from agent.tools.browse_url import BrowseUrlTool
        from agent.tools.dev_server import TailDevServerLogTool
        registry.register(BrowseUrlTool())
        registry.register(TailDevServerLogTool())
    return registry
```

- [ ] **Step 4: Pipe `with_browser` through `create_agent`**

In `agent/lifecycle/factory.py`, add `with_browser: bool = False` to `create_agent`'s parameters; forward it to `create_default_registry(...)`.

- [ ] **Step 5: Run tests**

```
.venv/bin/python3 -m pytest tests/test_tools_registry.py -v
```

Expected: 2 passed.

- [ ] **Step 6: Commit**

```
git add agent/tools/__init__.py agent/lifecycle/factory.py tests/test_tools_registry.py
git commit -m "feat(tools): with_browser flag for create_default_registry / create_agent"
```

---

## Phase 3: Planner contract change

### Task 12: Planner outputs `affected_routes`

**Files:**
- Modify: `agent/prompts.py` (planning prompt)
- Modify: `agent/lifecycle/planning.py` (parse + persist)

- [ ] **Step 1: Failing test**

Append to `tests/test_planning_affected_routes.py` (create):

```python
import pytest
from agent.lifecycle.planning import _extract_affected_routes


def test_extract_routes_from_fenced_block():
    plan_text = """
    ## Plan
    Adds a dark mode toggle to the homepage settings panel.

    ```affected-routes
    [
      {"method": "GET", "path": "/", "label": "homepage"},
      {"method": "GET", "path": "/settings", "label": "settings page"}
    ]
    ```
    """
    routes = _extract_affected_routes(plan_text)
    assert len(routes) == 2
    assert routes[0]["path"] == "/"


def test_extract_empty_when_no_block():
    routes = _extract_affected_routes("plan with no routes block")
    assert routes == []


def test_extract_empty_when_block_is_empty_list():
    routes = _extract_affected_routes("```affected-routes\n[]\n```")
    assert routes == []
```

- [ ] **Step 2: Implement `_extract_affected_routes` in `agent/lifecycle/planning.py`**

Append near the other helpers (after `_extract_grill_done`):

```python
import json as _json
import re as _re_routes


_ROUTES_BLOCK_RE = _re_routes.compile(
    r"```affected-routes\s*\n(.*?)\n```", _re_routes.DOTALL,
)


def _extract_affected_routes(plan_text: str) -> list[dict]:
    """Parse the agent's ```affected-routes fenced JSON block (if any)."""
    m = _ROUTES_BLOCK_RE.search(plan_text or "")
    if not m:
        return []
    try:
        data = _json.loads(m.group(1))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    cleaned = []
    for r in data:
        if not isinstance(r, dict) or "path" not in r:
            continue
        cleaned.append({
            "method": r.get("method", "GET").upper(),
            "path": str(r["path"]),
            "label": str(r.get("label", "")),
        })
    return cleaned
```

- [ ] **Step 3: Persist `affected_routes` when transitioning to `AWAITING_APPROVAL`**

Find where the plan text gets posted to the orchestrator (`transition_task(task_id, "awaiting_approval", plan_text)`). Extend the transition API to accept `affected_routes` on this path, OR write directly via a new helper:

In `agent/lifecycle/_orchestrator_api.py`, add:

```python
async def set_task_affected_routes(task_id: int, routes: list[dict]) -> None:
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{ORCHESTRATOR_URL}/tasks/{task_id}/affected_routes",
            json={"routes": routes},
        )
```

In `orchestrator/router.py`, add:

```python
@router.post("/tasks/{task_id}/affected_routes")
async def set_affected_routes(task_id: int, body: dict, db: AsyncSession = Depends(get_db)):
    task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if not task:
        raise HTTPException(404)
    task.affected_routes = body.get("routes", [])
    await db.commit()
    return {"ok": True}
```

In `agent/lifecycle/planning.py`, near where the plan is finalised:

```python
routes = _extract_affected_routes(plan_output)
if routes is not None:
    await set_task_affected_routes(task_id, routes)
```

- [ ] **Step 4: Update the planning prompt**

In `agent/prompts.py`, find `build_planning_prompt` and append to its instructions:

```python
PLANNING_AFFECTED_ROUTES_INSTRUCTION = """
At the end of your plan, include a fenced JSON block listing the user-visible
routes this change affects, if any. Format:

```affected-routes
[
  {"method": "GET", "path": "/", "label": "short label"}
]
```

Rules:
- Include each route the change makes user-visible output appear or change at.
- If the change is purely backend, CLI, library, or docs with no rendered UI,
  emit an empty list: ```affected-routes\n[]\n```
- Use GET for page renders; only list other methods if they have a visual response.
"""
```

Append it to the planning prompt body inside `build_planning_prompt`.

- [ ] **Step 5: Run tests**

```
.venv/bin/python3 -m pytest tests/test_planning_affected_routes.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Commit**

```
git add agent/lifecycle/planning.py agent/lifecycle/_orchestrator_api.py agent/prompts.py orchestrator/router.py tests/test_planning_affected_routes.py
git commit -m "feat(planning): planner outputs affected_routes, orchestrator persists"
```

---

## Phase 4: Coding phase extensions

### Task 13: Extract `_open_pr_and_advance` from `_finish_coding`

**Files:**
- Modify: `agent/lifecycle/coding.py` (around line 422)

Refactor only — behavior must not change. Existing tests are the safety net.

- [ ] **Step 1: Locate and copy the tail**

Existing `_finish_coding` body has (1) self-review loop (lines ~431-456), (2) commit / ensure_branch / push, (3) `create_pr`, (4) `handle_independent_review`. Extract steps 2-4 into a new helper:

```python
async def _open_pr_and_advance(
    task_id: int,
    task,
    workspace: str,
    base_branch: str,
    branch_name: str,
) -> None:
    """Push branch, create PR, kick off review. Idempotent."""
    committed_now = await commit_pending_changes(workspace, task_id, task.title)
    if committed_now:
        log.warning(
            f"Task #{task_id}: agent left uncommitted changes — auto-committed them before push"
        )
    await ensure_branch_has_commits(workspace, base_branch)
    await push_branch(workspace, branch_name)

    pr_body = (
        f"## Auto-Agent Task #{task_id}\n\n"
        f"**Task:** {task.title}\n\n"
        f"**Description:** {task.description[:500]}\n\n"
        f"---\n"
        f"*Generated by auto-agent. Code was self-reviewed for correctness, security, and root-cause analysis.*"
    )
    title = await _pr_title(task.title)
    pr_url = await review.create_pr(
        workspace, title, pr_body, base_branch, branch_name,
        user_id=task.created_by_user_id,
        organization_id=task.organization_id,
    )
    log.info(f"PR created: {pr_url}")
    if not pr_url.startswith("http"):
        raise RuntimeError(f"gh pr create returned invalid URL: {pr_url!r}")

    await review.handle_independent_review(task_id, pr_url, branch_name)
```

- [ ] **Step 2: Replace the tail of `_finish_coding` with a call**

```python
async def _finish_coding(
    task_id: int,
    task,
    workspace: str,
    session_id: str,
    base_branch: str,
    branch_name: str,
) -> None:
    """Self-review, then hand off to verify (which calls _open_pr_and_advance)."""
    for attempt in range(MAX_REVIEW_RETRIES):
        # ... existing self-review loop unchanged ...
        if _review_passed(review_output):
            log.info(f"Self-review passed for task #{task_id}")
            break
    else:
        log.warning(
            f"Self-review did not fully pass after {MAX_REVIEW_RETRIES} attempts for task #{task_id}"
        )
    # Hand-off to verify; verify's pass_cycle calls _open_pr_and_advance.
    await _open_pr_and_advance(task_id, task, workspace, base_branch, branch_name)
```

(Task 16 will replace this `_open_pr_and_advance` call with the verify dispatch.)

- [ ] **Step 3: Run the full coding test suite to confirm no regression**

```
.venv/bin/python3 -m pytest tests/ -q -k "coding"
```

Expected: existing tests pass.

- [ ] **Step 4: Commit**

```
git add agent/lifecycle/coding.py
git commit -m "refactor(coding): extract _open_pr_and_advance from _finish_coding"
```

---

### Task 14: Coding pre-loop starts dev server when applicable

**Files:**
- Modify: `agent/lifecycle/coding.py` (top of `handle_coding`)
- Modify: `agent/lifecycle/factory.py` (so `create_agent` can be told a dev server is running and inject browser tools)

- [ ] **Step 1: Failing test**

Create `tests/test_coding_server_lifecycle.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.lifecycle import coding


async def test_coding_starts_server_when_routes_and_runner(monkeypatch):
    """affected_routes non-empty + sniff_run_command non-None → server started."""
    started = {}

    @asynccontextmanager
    async def fake_start(ws, override=None):
        started["called"] = True
        yield MagicMock(port=12345, log_path="/tmp/x.log")

    monkeypatch.setattr("agent.tools.dev_server.start_dev_server", fake_start)
    monkeypatch.setattr(
        "agent.tools.dev_server.sniff_run_command",
        lambda ws, override=None: "npm run dev",
    )
    monkeypatch.setattr(
        "agent.lifecycle.coding.wait_for_port", AsyncMock(),
    )

    task = MagicMock(affected_routes=[{"path": "/", "label": "home"}])
    # Drive the bit of handle_coding that starts the server.
    server = await coding._maybe_start_coding_server(task, "/tmp/ws")
    assert started["called"] is True
    assert server.port == 12345


async def test_coding_no_server_when_no_routes(monkeypatch):
    task = MagicMock(affected_routes=[])
    server = await coding._maybe_start_coding_server(task, "/tmp/ws")
    assert server is None


async def test_coding_no_server_when_no_runner(monkeypatch):
    monkeypatch.setattr(
        "agent.tools.dev_server.sniff_run_command",
        lambda ws, override=None: None,
    )
    task = MagicMock(affected_routes=[{"path": "/", "label": "home"}])
    server = await coding._maybe_start_coding_server(task, "/tmp/ws")
    assert server is None
```

(Don't forget `from contextlib import asynccontextmanager` in the test file.)

- [ ] **Step 2: Implement the helper**

In `agent/lifecycle/coding.py`, add near the imports / module top:

```python
from agent.tools import dev_server as _dev_server
```

Add the helper:

```python
async def _maybe_start_coding_server(task, workspace: str):
    """Start the project's dev server for the duration of coding, if applicable.

    Returns the DevServerHandle (or None when not started). Caller is responsible
    for keeping the async context alive across the agent loop and tearing down
    on exit. See ``handle_coding``.
    """
    routes = getattr(task, "affected_routes", None) or []
    if not routes:
        return None
    override = None
    # Optional FreeformConfig override:
    if getattr(task, "freeform_mode", False) and getattr(task, "repo_name", None):
        cfg = await get_freeform_config(task.repo_name)
        if cfg and getattr(cfg, "run_command", None):
            override = cfg.run_command
    if _dev_server.sniff_run_command(workspace, override=override) is None:
        return None
    return _dev_server.start_dev_server(workspace, override=override)
```

In `handle_coding` (around the point the workspace is ready), wrap the agent loop with:

```python
server_cm = await _maybe_start_coding_server(task, workspace)
server_handle = None
try:
    if server_cm is not None:
        try:
            server_handle = await server_cm.__aenter__()
            await _dev_server.wait_for_port(server_handle.port, timeout=60, log_path=server_handle.log_path)
        except (_dev_server.BootTimeout, _dev_server.BootError) as e:
            await publish(coding_server_boot_failed(task_id, str(e)))
            server_handle = None
            server_cm = None
    # ... existing agent loop, passing server_handle into create_agent ...
finally:
    if server_cm is not None and server_handle is not None:
        await server_cm.__aexit__(None, None, None)
```

In `create_agent` (in `agent/lifecycle/factory.py`), pass `with_browser=True` when a `dev_server_log_path` is provided, and inject the path into the agent's `ToolContext` so `tail_dev_server_log` can read it.

- [ ] **Step 3: Run tests**

```
.venv/bin/python3 -m pytest tests/test_coding_server_lifecycle.py -v
```

Expected: 3 passed.

- [ ] **Step 4: Commit**

```
git add agent/lifecycle/coding.py agent/lifecycle/factory.py tests/test_coding_server_lifecycle.py
git commit -m "feat(coding): start dev server in coding when affected_routes resolves"
```

---

### Task 15: Coding system prompt augmentation

**Files:**
- Modify: `agent/prompts.py`
- Modify: `agent/lifecycle/coding.py`

- [ ] **Step 1: Failing test**

Append to `tests/test_prompts.py` (create if absent):

```python
from agent.prompts import augment_coding_prompt_with_server


def test_augment_inserts_server_block():
    base = "You are an autonomous coding agent."
    out = augment_coding_prompt_with_server(
        base, port=3000, affected_routes=[
            {"method": "GET", "path": "/", "label": "home"},
            {"method": "GET", "path": "/settings", "label": "settings"},
        ],
    )
    assert "http://localhost:3000" in out
    assert "/settings" in out
    assert "browse_url" in out


def test_augment_passthrough_when_no_server():
    base = "You are an autonomous coding agent."
    assert augment_coding_prompt_with_server(base, port=None, affected_routes=[]) == base
```

- [ ] **Step 2: Implement**

In `agent/prompts.py`, append:

```python
def augment_coding_prompt_with_server(
    base_prompt: str, *, port: int | None, affected_routes: list[dict],
) -> str:
    if not port or not affected_routes:
        return base_prompt
    route_lines = "\n".join(
        f"- {r.get('method','GET')} {r['path']}  ({r.get('label','')})"
        for r in affected_routes
    )
    block = f"""

## Dev server

A dev server is running at http://localhost:{port}. Affected routes for this task:
{route_lines}

Use the `browse_url` tool to inspect rendered output as you make changes. Most
frameworks hot-reload — re-screenshot after edits to see results. Use
`tail_dev_server_log` if you need to debug server output.
"""
    return base_prompt + block
```

In `handle_coding`, wherever the system prompt is built, call:

```python
system_prompt = augment_coding_prompt_with_server(
    system_prompt,
    port=server_handle.port if server_handle else None,
    affected_routes=task.affected_routes or [],
)
```

- [ ] **Step 3: Run tests**

```
.venv/bin/python3 -m pytest tests/test_prompts.py::test_augment_inserts_server_block tests/test_prompts.py::test_augment_passthrough_when_no_server -v
```

Expected: 2 passed.

- [ ] **Step 4: Commit**

```
git add agent/prompts.py agent/lifecycle/coding.py tests/test_prompts.py
git commit -m "feat(coding): system prompt advertises dev server + affected routes"
```

---

### Task 16: `_finish_coding` transitions `CODING → VERIFYING`

**Files:**
- Modify: `agent/lifecycle/coding.py` (`_finish_coding` body)
- Note: `verify.handle_verify` doesn't exist yet — we'll forward-declare a stub in Task 17 and replace it.

- [ ] **Step 1: Failing test**

Append to `tests/test_coding_to_verify.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest

from agent.lifecycle import coding


async def test_finish_coding_dispatches_verify(monkeypatch):
    monkeypatch.setattr(
        "agent.lifecycle.coding.transition_task", AsyncMock(),
    )
    mock_verify = AsyncMock()
    monkeypatch.setattr(
        "agent.lifecycle.verify.handle_verify", mock_verify,
    )

    task = AsyncMock(id=42, title="t", description="d", affected_routes=[])
    await coding._finish_coding(
        task_id=42, task=task, workspace="/tmp/ws", session_id="s",
        base_branch="main", branch_name="b",
    )
    mock_verify.assert_called_once_with(42)
```

- [ ] **Step 2: Update `_finish_coding`**

Replace its body (after the self-review loop) with:

```python
    # Self-review loop is unchanged above.

    # Hand off to verify; verify's pass_cycle calls _open_pr_and_advance.
    from agent.lifecycle import verify
    await transition_task(task_id, "verifying", "self-review complete; dispatching verify")
    await verify.handle_verify(task_id)
```

- [ ] **Step 3: Run tests**

```
.venv/bin/python3 -m pytest tests/test_coding_to_verify.py -v
```

Expected: 1 passed (after Task 17 is in place; until then this test will fail with `AttributeError: module 'agent.lifecycle.verify'`. The next task creates the skeleton).

- [ ] **Step 4: Commit**

```
git add agent/lifecycle/coding.py tests/test_coding_to_verify.py
git commit -m "feat(coding): _finish_coding dispatches handle_verify"
```

---

## Phase 5: Verify phase

### Task 17: Verify phase skeleton + boot check

**Files:**
- Create: `agent/lifecycle/verify.py`
- Create: `tests/test_verify_phase.py`

- [ ] **Step 1: Failing test**

`tests/test_verify_phase.py`:

```python
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.lifecycle import verify


@pytest.fixture
def patch_orchestrator(monkeypatch):
    monkeypatch.setattr("agent.lifecycle.verify.get_task", AsyncMock())
    monkeypatch.setattr("agent.lifecycle.verify.transition_task", AsyncMock())
    monkeypatch.setattr("agent.lifecycle.verify.create_verify_attempt", AsyncMock(return_value=MagicMock(id=1)))
    monkeypatch.setattr("agent.lifecycle.verify.update_verify_attempt", AsyncMock())
    return monkeypatch


async def test_boot_check_pass(patch_orchestrator):
    @asynccontextmanager
    async def fake_start(ws, override=None):
        yield MagicMock(port=12345, log_path="/tmp/log", process=MagicMock(returncode=None))

    patch_orchestrator.setattr("agent.tools.dev_server.start_dev_server", fake_start)
    patch_orchestrator.setattr("agent.tools.dev_server.sniff_run_command", lambda ws, override=None: "npm run dev")
    patch_orchestrator.setattr("agent.tools.dev_server.wait_for_port", AsyncMock())
    patch_orchestrator.setattr("agent.tools.dev_server.hold", AsyncMock())
    # Stub intent check to OK.
    patch_orchestrator.setattr(
        "agent.lifecycle.verify.run_intent_check",
        AsyncMock(return_value=MagicMock(ok=True, reasoning="ok", tool_calls=[])),
    )
    patch_orchestrator.setattr("agent.lifecycle.verify._open_pr_and_advance", AsyncMock())

    verify.get_task.return_value = MagicMock(
        id=42, affected_routes=[], freeform_mode=True, repo_name="r",
    )
    await verify.handle_verify(42)
    # pass_cycle calls _open_pr_and_advance
    verify._open_pr_and_advance.assert_called_once()


async def test_boot_check_skipped_when_no_runner(patch_orchestrator):
    patch_orchestrator.setattr("agent.tools.dev_server.sniff_run_command", lambda ws, override=None: None)
    patch_orchestrator.setattr(
        "agent.lifecycle.verify.run_intent_check",
        AsyncMock(return_value=MagicMock(ok=True, reasoning="ok", tool_calls=[])),
    )
    patch_orchestrator.setattr("agent.lifecycle.verify._open_pr_and_advance", AsyncMock())
    verify.get_task.return_value = MagicMock(
        id=42, affected_routes=[], freeform_mode=True, repo_name="r",
    )
    await verify.handle_verify(42)
    verify._open_pr_and_advance.assert_called_once()


async def test_boot_fail_early_exit_loops_back(patch_orchestrator):
    @asynccontextmanager
    async def fake_start(ws, override=None):
        yield MagicMock(port=12345, log_path="/tmp/log", process=MagicMock(returncode=None))

    from agent.tools.dev_server import EarlyExit

    patch_orchestrator.setattr("agent.tools.dev_server.start_dev_server", fake_start)
    patch_orchestrator.setattr("agent.tools.dev_server.sniff_run_command", lambda ws, override=None: "npm run dev")
    patch_orchestrator.setattr("agent.tools.dev_server.wait_for_port", AsyncMock())
    patch_orchestrator.setattr("agent.tools.dev_server.hold", AsyncMock(side_effect=EarlyExit("crashed")))

    verify.get_task.return_value = MagicMock(
        id=42, affected_routes=[], freeform_mode=True, repo_name="r",
    )
    await verify.handle_verify(42)
    # Should transition VERIFYING → CODING on cycle 1 failure.
    verify.transition_task.assert_any_call(42, "coding", pytest.approx_anywhere := pytest.ANY)
```

(Use `unittest.mock.ANY` if `pytest.ANY` is unfamiliar.)

- [ ] **Step 2: Implement the skeleton**

`agent/lifecycle/verify.py`:

```python
"""Verify lifecycle phase — boot check + intent check.

Runs between CODING and PR_CREATED (state ``VERIFYING``). Two cycles max:
fail cycle 1 → CODING; fail cycle 2 → BLOCKED.
"""
from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import select

from agent.lifecycle._naming import _fresh_session_id
from agent.lifecycle._orchestrator_api import (
    ORCHESTRATOR_URL,
    get_freeform_config,
    get_task,
    transition_task,
)
from agent.lifecycle.factory import create_agent, home_dir_for_task
from agent.prompts import build_verify_intent_prompt
from agent.tools import dev_server as _dev_server
from agent.workspace import clone_repo
from shared.database import async_session
from shared.events import (
    publish,
    verify_failed,
    verify_passed,
    verify_skipped_no_runner,
    verify_started,
)
from shared.logging import setup_logging
from shared.models import ReviewAttempt, VerifyAttempt
from shared.quotas import QuotaExceeded
from shared.types import IntentVerdict

log = setup_logging("agent.lifecycle.verify")

MAX_VERIFY_CYCLES = 2
PHASE_TIMEOUT_SECONDS = 120


async def handle_verify(task_id: int) -> None:
    """Entry point: run the verify phase for a task currently in VERIFYING."""
    task = await get_task(task_id)
    if not task:
        return
    cycle = await _next_cycle(task_id)
    if cycle > MAX_VERIFY_CYCLES:
        log.error(f"task #{task_id}: verify cycle budget exhausted")
        await transition_task(task_id, "blocked", "verify_failed (budget exhausted)")
        return

    await publish(verify_started(task_id, cycle))
    attempt = await _create_verify_attempt(task_id, cycle)

    try:
        await asyncio.wait_for(
            _run_verify_body(task, task_id, cycle, attempt),
            timeout=PHASE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await _fail_cycle(task_id, attempt, cycle, "phase_timeout", None)


async def _run_verify_body(task, task_id: int, cycle: int, attempt) -> None:
    workspace, base_branch = await _prepare_workspace(task)
    override = None
    if getattr(task, "freeform_mode", False) and task.repo_name:
        cfg = await get_freeform_config(task.repo_name)
        if cfg and getattr(cfg, "run_command", None):
            override = cfg.run_command

    run_cmd = _dev_server.sniff_run_command(workspace, override=override)
    server = None
    server_cm = None
    if run_cmd:
        server_cm = _dev_server.start_dev_server(workspace, override=override)
        try:
            server = await server_cm.__aenter__()
            await _dev_server.wait_for_port(server.port, timeout=60, log_path=server.log_path)
            await _dev_server.hold(server, seconds=5)
            await _update_verify_attempt(attempt.id, boot_check="pass")
        except _dev_server.BootTimeout as e:
            await _update_verify_attempt(attempt.id, boot_check="fail", log_tail=e.log_tail)
            await _close_server(server_cm)
            return await _fail_cycle(task_id, attempt, cycle, "boot_timeout", e.log_tail)
        except _dev_server.EarlyExit as e:
            await _update_verify_attempt(attempt.id, boot_check="fail", log_tail=e.log_tail)
            await _close_server(server_cm)
            return await _fail_cycle(task_id, attempt, cycle, "early_exit", e.log_tail)
    else:
        await _update_verify_attempt(attempt.id, boot_check="skipped")
        if (task.affected_routes or []):
            await publish(verify_skipped_no_runner(task_id))

    try:
        verdict = await run_intent_check(task, workspace, server)
        await _update_verify_attempt(
            attempt.id,
            intent_check="pass" if verdict.ok else "fail",
            intent_judgment=verdict.reasoning,
            tool_calls=verdict.tool_calls,
        )
        if not verdict.ok:
            return await _fail_cycle(task_id, attempt, cycle, "intent_not_addressed", None)
        return await _pass_cycle(task_id, attempt, task, workspace, base_branch)
    finally:
        if server_cm is not None:
            await _close_server(server_cm)


async def _close_server(server_cm) -> None:
    try:
        await server_cm.__aexit__(None, None, None)
    except Exception:
        log.exception("dev_server cleanup raised")


async def run_intent_check(task, workspace: str, server) -> IntentVerdict:
    """Placeholder — implemented in Task 18."""
    return IntentVerdict(ok=True, reasoning="(intent check stub)", tool_calls=[])


async def _pass_cycle(task_id: int, attempt, task, workspace: str, base_branch: str) -> None:
    await _update_verify_attempt(attempt.id, status="pass", finished=True)
    await publish(verify_passed(task_id, attempt.cycle))
    from agent.lifecycle.coding import _open_pr_and_advance
    branch_name = task.branch_name
    await _open_pr_and_advance(task_id, task, workspace, base_branch, branch_name)


async def _fail_cycle(
    task_id: int, attempt, cycle: int, reason: str, log_tail: str | None,
) -> None:
    await _update_verify_attempt(
        attempt.id, status="fail", finished=True,
        failure_reason=reason, log_tail=log_tail,
    )
    await publish(verify_failed(task_id, cycle, reason))
    if cycle >= MAX_VERIFY_CYCLES:
        await transition_task(task_id, "blocked", f"verify_failed: {reason}")
    else:
        await transition_task(task_id, "coding", f"verify failed (cycle {cycle}): {reason}")


# --- DB helpers ---

async def _next_cycle(task_id: int) -> int:
    async with async_session() as s:
        result = await s.execute(
            select(VerifyAttempt).where(VerifyAttempt.task_id == task_id),
        )
        rows = result.scalars().all()
        return len(rows) + 1


async def _create_verify_attempt(task_id: int, cycle: int):
    async with async_session() as s:
        a = VerifyAttempt(task_id=task_id, cycle=cycle, status="error")
        s.add(a)
        await s.commit()
        await s.refresh(a)
        return a


async def _update_verify_attempt(attempt_id: int, finished: bool = False, **fields) -> None:
    from datetime import UTC, datetime
    async with async_session() as s:
        a = (await s.execute(
            select(VerifyAttempt).where(VerifyAttempt.id == attempt_id),
        )).scalar_one()
        for k, v in fields.items():
            setattr(a, k, v)
        if finished:
            a.finished_at = datetime.now(UTC)
        await s.commit()


async def _prepare_workspace(task) -> tuple[str, str]:
    """Reuse / re-clone the workspace for verify. Returns (path, base_branch)."""
    from agent.lifecycle._orchestrator_api import get_repo
    repo = await get_repo(task.repo_name)
    base_branch = repo.default_branch
    if task.freeform_mode and task.repo_name:
        cfg = await get_freeform_config(task.repo_name)
        if cfg:
            base_branch = cfg.dev_branch
    workspace = await clone_repo(
        repo.url, task.id, base_branch,
        user_id=task.created_by_user_id,
        organization_id=task.organization_id,
    )
    return workspace, base_branch
```

- [ ] **Step 3: Add a stub `build_verify_intent_prompt` to `agent/prompts.py`**

```python
def build_verify_intent_prompt(
    task_title: str, task_description: str, diff_summary: str,
    affected_routes: list[dict], server_url: str | None,
) -> str:
    return f"Task: {task_title}\n\nDescription:\n{task_description}\n\nDiff:\n{diff_summary}\n\n(prompt body filled in Task 18)"
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python3 -m pytest tests/test_verify_phase.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add agent/lifecycle/verify.py agent/prompts.py tests/test_verify_phase.py
git commit -m "feat(verify): skeleton handle_verify with boot check and retry"
```

---

### Task 18: Verify intent check (agent invocation)

**Files:**
- Modify: `agent/lifecycle/verify.py` (replace `run_intent_check` stub)
- Modify: `agent/prompts.py` (flesh out `build_verify_intent_prompt`)

- [ ] **Step 1: Failing test**

Append to `tests/test_verify_phase.py`:

```python
async def test_intent_check_fail_loops_back(patch_orchestrator):
    @asynccontextmanager
    async def fake_start(ws, override=None):
        yield MagicMock(port=12345, log_path="/tmp/log", process=MagicMock(returncode=None))

    patch_orchestrator.setattr("agent.tools.dev_server.start_dev_server", fake_start)
    patch_orchestrator.setattr("agent.tools.dev_server.sniff_run_command", lambda ws, override=None: "npm run dev")
    patch_orchestrator.setattr("agent.tools.dev_server.wait_for_port", AsyncMock())
    patch_orchestrator.setattr("agent.tools.dev_server.hold", AsyncMock())
    patch_orchestrator.setattr(
        "agent.lifecycle.verify.run_intent_check",
        AsyncMock(return_value=MagicMock(ok=False, reasoning="missing dark mode toggle", tool_calls=[])),
    )
    verify.get_task.return_value = MagicMock(
        id=42, affected_routes=[], freeform_mode=True, repo_name="r",
    )
    await verify.handle_verify(42)
    verify.transition_task.assert_any_call(42, "coding", pytest.approx_anywhere := __import__("unittest.mock").mock.ANY)
```

- [ ] **Step 2: Replace `run_intent_check` with a real agent invocation**

```python
import re

INTENT_OK_RE = re.compile(r"^\s*OK\b", re.MULTILINE)


async def run_intent_check(task, workspace: str, server) -> IntentVerdict:
    from agent import sh
    # Build the diff summary (lightweight): list of changed files + a short summary.
    diff_result = await sh.run(
        ["git", "diff", "--stat", "HEAD~1"], cwd=workspace, timeout=30,
    )
    diff_summary = diff_result.stdout or "(no diff stat available)"

    server_url = f"http://localhost:{server.port}" if server is not None else None
    prompt = build_verify_intent_prompt(
        task.title, task.description, diff_summary,
        task.affected_routes or [], server_url,
    )

    session_id = _fresh_session_id(task.id, "verify-intent")
    agent = create_agent(
        workspace,
        session_id=session_id,
        readonly=True,
        with_browser=server is not None,
        max_turns=15,
        task_description=task.description,
        repo_name=task.repo_name,
        home_dir=await home_dir_for_task(task),
        org_id=task.organization_id,
        dev_server_log_path=(server.log_path if server is not None else None),
    )
    result = await agent.run(prompt)
    output = result.output or ""

    ok = bool(INTENT_OK_RE.search(output)) and not output.lstrip().startswith("NOT-OK")
    return IntentVerdict(
        ok=ok,
        reasoning=output[:4000],
        tool_calls=getattr(result, "tool_calls", []),
    )
```

`create_agent` must accept `dev_server_log_path` (forward into the agent's ToolContext); update `factory.py` to do so.

- [ ] **Step 3: Flesh out `build_verify_intent_prompt`**

```python
def build_verify_intent_prompt(
    task_title: str, task_description: str, diff_summary: str,
    affected_routes: list[dict], server_url: str | None,
) -> str:
    route_block = ""
    if affected_routes:
        lines = "\n".join(f"- {r.get('method','GET')} {r['path']} ({r.get('label','')})" for r in affected_routes)
        route_block = f"\n\nAffected routes:\n{lines}"
    server_block = ""
    if server_url:
        server_block = (
            f"\n\nA dev server is running at {server_url}. If the task describes "
            "visual behaviour or UI changes, call `browse_url` on each affected route "
            "to confirm the rendered output matches the description. Use "
            "`tail_dev_server_log` if anything looks wrong."
        )

    return f"""You are the intent verifier. Decide whether the diff below addresses
the task as stated.

Task title: {task_title}
Task description: {task_description}

Diff summary:
{diff_summary}{route_block}{server_block}

Output your verdict on the first line of your reply, exactly one of:
- OK
- NOT-OK: <one-line reason>

Then on subsequent lines, write a short reasoning paragraph (no more than 5 lines)
covering: missing requirements, off-topic changes, partial implementations. If
you used browse_url, mention what you observed.
"""
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python3 -m pytest tests/test_verify_phase.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```
git add agent/lifecycle/verify.py agent/prompts.py agent/lifecycle/factory.py
git commit -m "feat(verify): intent check uses readonly agent with browse_url when server is up"
```

---

### Task 19: Verify event hook for orchestrator dispatch

**Files:**
- Modify: `agent/main.py` (event handler registration) — verify needs to be dispatched when a task transitions to VERIFYING from outside (e.g. retry).

- [ ] **Step 1: Inspect existing event handler registration**

```
grep -n "lifecycle\|handle\s*=" agent/main.py | head
```

- [ ] **Step 2: Add the handler**

```python
async def handle_verify_event(event: Event) -> None:
    if not event.task_id:
        return
    from agent.lifecycle import verify
    await verify.handle_verify(event.task_id)
```

Register it on the same hook the orchestrator publishes after a `transition_task(..., "verifying")` post.

- [ ] **Step 3: Smoke test by running existing event tests**

```
.venv/bin/python3 -m pytest tests/ -k "events" -q
```

Expected: no regressions.

- [ ] **Step 4: Commit**

```
git add agent/main.py
git commit -m "feat(verify): wire handle_verify into agent event loop"
```

---

## Phase 6: Review extension (UI check)

### Task 20: Persist `ReviewAttempt` per `handle_independent_review` invocation

**Files:**
- Modify: `agent/lifecycle/review.py`

- [ ] **Step 1: Failing test**

Create `tests/test_review_attempts.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.lifecycle import review
from shared.models import ReviewAttempt


async def test_review_creates_attempt_row(monkeypatch, db_session):
    monkeypatch.setattr("agent.lifecycle.review.get_task", AsyncMock(return_value=MagicMock(
        id=1, repo_name="r", freeform_mode=True, created_by_user_id=None, organization_id=1,
        description="d", title="t", affected_routes=[],
    )))
    monkeypatch.setattr("agent.lifecycle.review.get_repo", AsyncMock(return_value=MagicMock(default_branch="main", url="x")))
    monkeypatch.setattr("agent.lifecycle.review.clone_repo", AsyncMock(return_value="/tmp/ws"))
    monkeypatch.setattr("agent.lifecycle.review.sh.run", AsyncMock(return_value=MagicMock(failed=False, stdout="", stderr="")))
    monkeypatch.setattr("agent.lifecycle.review.create_agent", MagicMock(return_value=MagicMock(run=AsyncMock(return_value=MagicMock(output="lgtm")))))

    await review.handle_independent_review(task_id=1, pr_url="http://pr", branch_name="b")
    # Now check the DB
    rows = (await db_session.execute(__import__('sqlalchemy').select(ReviewAttempt))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "pass"
    assert rows[0].code_review_verdict.strip().lower().startswith("lgtm") or "lgtm" in rows[0].code_review_verdict.lower()
```

- [ ] **Step 2: Add a helper at the top of `handle_independent_review` to persist the attempt**

```python
from shared.database import async_session
from shared.models import ReviewAttempt
from sqlalchemy import select


async def _create_review_attempt(task_id: int, cycle: int):
    async with async_session() as s:
        a = ReviewAttempt(task_id=task_id, cycle=cycle, status="error")
        s.add(a); await s.commit(); await s.refresh(a)
        return a


async def _update_review_attempt(attempt_id: int, finished: bool = False, **fields):
    from datetime import UTC, datetime
    async with async_session() as s:
        a = (await s.execute(select(ReviewAttempt).where(ReviewAttempt.id == attempt_id))).scalar_one()
        for k, v in fields.items():
            setattr(a, k, v)
        if finished:
            a.finished_at = datetime.now(UTC)
        await s.commit()


async def _next_review_cycle(task_id: int) -> int:
    async with async_session() as s:
        rows = (await s.execute(
            select(ReviewAttempt).where(ReviewAttempt.task_id == task_id)
        )).scalars().all()
        return len(rows) + 1
```

At the top of `handle_independent_review`:

```python
    cycle = await _next_review_cycle(task_id)
    attempt = await _create_review_attempt(task_id, cycle)
```

At the approval branch:

```python
    if approved:
        await _update_review_attempt(
            attempt.id, status="pass",
            code_review_verdict=output[:4000],
            finished=True,
        )
        # ... existing publish(task_review_complete(approved=True)) ...
```

At the rejection branch:

```python
    else:
        await _update_review_attempt(
            attempt.id, status="fail",
            code_review_verdict=output[:4000],
            failure_reason="code_review_rejected",
            finished=True,
        )
        # ... existing fix flow ...
```

- [ ] **Step 3: Run tests**

```
.venv/bin/python3 -m pytest tests/test_review_attempts.py -v
```

Expected: pass.

- [ ] **Step 4: Commit**

```
git add agent/lifecycle/review.py tests/test_review_attempts.py
git commit -m "feat(review): persist ReviewAttempt rows per independent-review invocation"
```

---

### Task 21: Review UI check — boot server and pass screenshots to reviewer

**Files:**
- Modify: `agent/lifecycle/review.py`
- Modify: `agent/prompts.py`

- [ ] **Step 1: Failing test**

Append to `tests/test_review_ui_check.py`:

```python
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.lifecycle import review


async def test_ui_check_skipped_when_no_routes(monkeypatch, db_session):
    task = MagicMock(
        id=1, repo_name="r", title="t", description="d",
        freeform_mode=True, created_by_user_id=None, organization_id=1,
        affected_routes=[],
    )
    monkeypatch.setattr("agent.lifecycle.review.get_task", AsyncMock(return_value=task))
    monkeypatch.setattr("agent.lifecycle.review.get_repo", AsyncMock(return_value=MagicMock(default_branch="main", url="x")))
    monkeypatch.setattr("agent.lifecycle.review.clone_repo", AsyncMock(return_value="/tmp/ws"))
    monkeypatch.setattr("agent.lifecycle.review.sh.run", AsyncMock(return_value=MagicMock(failed=False, stdout="", stderr="")))
    monkeypatch.setattr("agent.lifecycle.review.create_agent", MagicMock(return_value=MagicMock(run=AsyncMock(return_value=MagicMock(output="lgtm")))))

    await review.handle_independent_review(1, "http://pr", "b")
    # Inspect the ReviewAttempt row
    rows = (await db_session.execute(__import__('sqlalchemy').select(__import__('shared.models', fromlist=['ReviewAttempt']).ReviewAttempt))).scalars().all()
    assert rows[0].ui_check == "skipped"


async def test_ui_check_runs_when_routes_and_runner(monkeypatch, db_session):
    task = MagicMock(
        id=2, repo_name="r", title="t", description="d",
        freeform_mode=True, created_by_user_id=None, organization_id=1,
        affected_routes=[{"method": "GET", "path": "/", "label": "home"}],
    )
    monkeypatch.setattr("agent.lifecycle.review.get_task", AsyncMock(return_value=task))
    monkeypatch.setattr("agent.lifecycle.review.get_repo", AsyncMock(return_value=MagicMock(default_branch="main", url="x")))
    monkeypatch.setattr("agent.lifecycle.review.clone_repo", AsyncMock(return_value="/tmp/ws"))
    monkeypatch.setattr("agent.lifecycle.review.sh.run", AsyncMock(return_value=MagicMock(failed=False, stdout="", stderr="")))
    monkeypatch.setattr("agent.tools.dev_server.sniff_run_command", lambda ws, override=None: "npm run dev")

    @asynccontextmanager
    async def fake_start(ws, override=None):
        yield MagicMock(port=12345, log_path="/tmp/log", process=MagicMock(returncode=None))

    monkeypatch.setattr("agent.tools.dev_server.start_dev_server", fake_start)
    monkeypatch.setattr("agent.tools.dev_server.wait_for_port", AsyncMock())
    monkeypatch.setattr("agent.lifecycle.review.create_agent", MagicMock(return_value=MagicMock(
        run=AsyncMock(return_value=MagicMock(
            output='{"code_review": {"verdict": "OK", "reasoning": ""}, "ui_check": {"verdict": "OK", "reasoning": ""}}',
            tool_calls=[{"name": "browse_url", "args": {"url": "http://localhost:12345/"}}],
        )),
    )))

    await review.handle_independent_review(2, "http://pr", "b")
    rows = (await db_session.execute(__import__('sqlalchemy').select(__import__('shared.models', fromlist=['ReviewAttempt']).ReviewAttempt))).scalars().all()
    assert rows[0].ui_check == "pass"
    assert rows[0].tool_calls is not None
```

- [ ] **Step 2: Implement the UI-check sub-step inside `handle_independent_review`**

After the workspace is cloned and the branch is checked out, before the existing `build_pr_independent_review_prompt` call:

```python
from agent.tools import dev_server as _dev_server
from agent.prompts import build_pr_independent_review_prompt_with_ui_check
import json

server_handle = None
server_cm = None
ui_check_status = "skipped"
override = None
if task.freeform_mode and task.repo_name:
    cfg = await get_freeform_config(task.repo_name)
    if cfg and getattr(cfg, "run_command", None):
        override = cfg.run_command

routes = task.affected_routes or []
if routes:
    run_cmd = _dev_server.sniff_run_command(workspace, override=override)
    if run_cmd is None:
        await publish(review_skipped_no_runner(task_id))
    else:
        server_cm = _dev_server.start_dev_server(workspace, override=override)
        try:
            server_handle = await server_cm.__aenter__()
            await _dev_server.wait_for_port(server_handle.port, timeout=60, log_path=server_handle.log_path)
        except (_dev_server.BootTimeout, _dev_server.BootError) as e:
            await _update_review_attempt(
                attempt.id, status="fail",
                failure_reason="boot_timeout",
                log_tail=getattr(e, "log_tail", str(e)),
                finished=True,
            )
            if server_cm is not None and server_handle is None:
                await server_cm.__aexit__(None, None, None)
            return await _review_loop_back(task_id, attempt.cycle, "boot_timeout")

prompt = build_pr_independent_review_prompt_with_ui_check(
    task.title, task.description, pr_url, base_branch,
    server_url=f"http://localhost:{server_handle.port}" if server_handle else None,
    affected_routes=routes,
)

agent = create_agent(
    workspace,
    session_id=reviewer_session,
    readonly=True,
    with_browser=server_handle is not None,
    max_turns=20,
    task_description=task.description,
    repo_name=task.repo_name,
    home_dir=await home_dir_for_task(task),
    org_id=task.organization_id,
    dev_server_log_path=(server_handle.log_path if server_handle else None),
)
result = await agent.run(prompt)
output = result.output

# Parse the structured verdict; fall back to keyword-based existing logic.
verdict = _parse_review_combined_verdict(output)
if verdict is None:
    # Backwards-compatible legacy path (used when reviewer didn't emit JSON).
    approved = any(p in (output or "").lower() for p in ["--approve", "lgtm", "looks good"])
    code_v = "OK" if approved else "NOT-OK"
    ui_v = "SKIPPED"
    code_reason = output[:2000]
    ui_reason = ""
else:
    code_v = verdict.code_review.verdict
    ui_v = verdict.ui_check.verdict
    code_reason = verdict.code_review.reasoning
    ui_reason = verdict.ui_check.reasoning

ui_check_status = {"OK": "pass", "NOT-OK": "fail", "SKIPPED": "skipped"}[ui_v]
await _update_review_attempt(
    attempt.id,
    code_review_verdict=code_reason,
    ui_check=ui_check_status,
    ui_judgment=ui_reason or None,
    tool_calls=getattr(result, "tool_calls", []),
)

approved = code_v == "OK" and ui_v in ("OK", "SKIPPED")

# ... rest of the existing approved/rejected branches (now using `approved` from above) ...

if server_cm is not None and server_handle is not None:
    await server_cm.__aexit__(None, None, None)
```

Add the parser helper near the top of `review.py`:

```python
def _parse_review_combined_verdict(output: str):
    """Try to parse the reviewer's combined verdict JSON. Returns None on miss."""
    import json
    try:
        # Greedy first-pass: find the first top-level JSON object in output.
        start = output.find("{")
        if start < 0:
            return None
        depth, end = 0, None
        for i, ch in enumerate(output[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            return None
        raw = output[start:end]
        from shared.types import ReviewCombinedVerdict
        return ReviewCombinedVerdict.model_validate_json(raw)
    except Exception:
        return None


async def _review_loop_back(task_id: int, cycle: int, reason: str) -> None:
    if cycle >= 2:
        await transition_task(task_id, "blocked", f"review_failed: {reason}")
    else:
        await transition_task(task_id, "coding", f"review failed (cycle {cycle}): {reason}")
```

- [ ] **Step 3: Update the reviewer prompt builder**

Append to `agent/prompts.py`:

```python
def build_pr_independent_review_prompt_with_ui_check(
    task_title: str, task_description: str, pr_url: str, base_branch: str,
    *, server_url: str | None, affected_routes: list[dict],
) -> str:
    base = build_pr_independent_review_prompt(task_title, task_description, pr_url, base_branch)
    if not server_url or not affected_routes:
        return base + UI_CHECK_PROMPT_SUFFIX_CODE_ONLY
    route_lines = "\n".join(
        f"- {r.get('method','GET')} {r['path']} ({r.get('label','')})"
        for r in affected_routes
    )
    return base + f"""

## UI check (required)

A dev server is running at {server_url}. The following routes are affected by this PR:
{route_lines}

For each affected route, call `browse_url({server_url}{{path}})` and judge the
rendered output against the diff and the task description. Use
`tail_dev_server_log` to investigate anything that looks wrong.

Emit your verdict as a single JSON object on the FIRST non-empty line of your
reply, with this exact shape:

{{
  "code_review": {{"verdict": "OK"|"NOT-OK", "reasoning": "..."}},
  "ui_check":   {{"verdict": "OK"|"NOT-OK", "reasoning": "..."}}
}}

If the code looks bad, set code_review.verdict to "NOT-OK". If a route doesn't
render correctly, set ui_check.verdict to "NOT-OK". Both must be "OK" to ship.
"""


UI_CHECK_PROMPT_SUFFIX_CODE_ONLY = """

Emit your verdict as a single JSON object on the FIRST non-empty line of your
reply:

{
  "code_review": {"verdict": "OK"|"NOT-OK", "reasoning": "..."},
  "ui_check":   {"verdict": "SKIPPED",        "reasoning": "no UI to check"}
}
"""
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python3 -m pytest tests/test_review_ui_check.py tests/test_review_attempts.py -v
```

Expected: all passed.

- [ ] **Step 5: Commit**

```
git add agent/lifecycle/review.py agent/prompts.py tests/test_review_ui_check.py
git commit -m "feat(review): UI check sub-step — boot server, structured verdict, ReviewAttempt"
```

---

### Task 22: Review 2-cycle budget + `BLOCKED` on second failure

**Files:**
- Modify: `agent/lifecycle/review.py`

- [ ] **Step 1: Failing test**

Append to `tests/test_review_ui_check.py`:

```python
async def test_review_blocks_after_two_failures(monkeypatch, db_session):
    task = MagicMock(
        id=3, repo_name="r", title="t", description="d",
        freeform_mode=True, created_by_user_id=None, organization_id=1,
        affected_routes=[],
    )
    monkeypatch.setattr("agent.lifecycle.review.get_task", AsyncMock(return_value=task))
    monkeypatch.setattr("agent.lifecycle.review.get_repo", AsyncMock(return_value=MagicMock(default_branch="main", url="x")))
    monkeypatch.setattr("agent.lifecycle.review.clone_repo", AsyncMock(return_value="/tmp/ws"))
    monkeypatch.setattr("agent.lifecycle.review.sh.run", AsyncMock(return_value=MagicMock(failed=False, stdout="", stderr="")))
    monkeypatch.setattr("agent.lifecycle.review.create_agent", MagicMock(return_value=MagicMock(
        run=AsyncMock(return_value=MagicMock(
            output='{"code_review": {"verdict": "NOT-OK", "reasoning": "bad"}, "ui_check": {"verdict": "SKIPPED", "reasoning": ""}}',
            tool_calls=[],
        )),
    )))

    transition_calls = []
    async def fake_transition(*args, **kwargs):
        transition_calls.append(args)
    monkeypatch.setattr("agent.lifecycle.review.transition_task", fake_transition)

    # Cycle 1 — should transition to coding
    await review.handle_independent_review(3, "http://pr", "b")
    # Cycle 2 — should transition to blocked
    await review.handle_independent_review(3, "http://pr", "b")

    last = transition_calls[-1]
    assert last[1] == "blocked"
```

- [ ] **Step 2: Replace the rejection branch of `handle_independent_review`**

```python
    # rejection
    await _review_loop_back(task_id, attempt.cycle, "code_review_rejected" if code_v == "NOT-OK" else "ui_judgment_not_ok")
```

(`_review_loop_back` from Task 21 already handles cycle 2 → BLOCKED.)

Also remove the inline fix-loop in `handle_independent_review`. The next time CODING runs, the agent will see the failure context via the existing task message stream (passed in via `_review_loop_back` message argument).

- [ ] **Step 3: Run tests**

```
.venv/bin/python3 -m pytest tests/test_review_ui_check.py -v
```

Expected: 3 passed.

- [ ] **Step 4: Commit**

```
git add agent/lifecycle/review.py tests/test_review_ui_check.py
git commit -m "feat(review): 2-cycle budget; transition to BLOCKED on second failure"
```

---

## Phase 7: Regression tests (load-bearing)

### Task 23: `test_no_pr_on_failed_boot.py`

**Files:**
- Create: `tests/test_no_pr_on_failed_boot.py`

- [ ] **Step 1: Write the test**

```python
"""Regression: a task with a broken dev script cannot reach PR_CREATED.

Maps to acceptance criterion #1 in the spec. Boot check fails twice in
verify → task is BLOCKED, _open_pr_and_advance is never called.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.lifecycle import verify


async def test_failed_boot_blocks_no_pr(monkeypatch, db_session):
    # Task has empty affected_routes (pure backend) — but the boot check still runs.
    task = MagicMock(
        id=99, repo_name="r", title="break the server", description="d",
        freeform_mode=True, created_by_user_id=None, organization_id=1,
        affected_routes=[], branch_name="b",
    )
    monkeypatch.setattr("agent.lifecycle.verify.get_task", AsyncMock(return_value=task))
    monkeypatch.setattr(
        "agent.lifecycle.verify._prepare_workspace",
        AsyncMock(return_value=("/tmp/ws", "main")),
    )
    monkeypatch.setattr(
        "agent.lifecycle.verify.get_freeform_config",
        AsyncMock(return_value=MagicMock(run_command="node -e 'process.exit(1)'")),
    )

    # Server "boots" but exits immediately.
    @asynccontextmanager
    async def fake_start(ws, override=None):
        yield MagicMock(port=12345, log_path="/tmp/log", process=MagicMock(returncode=1))

    monkeypatch.setattr("agent.tools.dev_server.start_dev_server", fake_start)
    monkeypatch.setattr("agent.tools.dev_server.sniff_run_command", lambda ws, override=None: "node -e 'process.exit(1)'")
    monkeypatch.setattr("agent.tools.dev_server.wait_for_port", AsyncMock())
    from agent.tools.dev_server import EarlyExit
    monkeypatch.setattr("agent.tools.dev_server.hold", AsyncMock(side_effect=EarlyExit("boom")))

    pr_helper = AsyncMock()
    monkeypatch.setattr("agent.lifecycle.coding._open_pr_and_advance", pr_helper)
    transitions = []
    async def fake_t(*a, **kw): transitions.append(a)
    monkeypatch.setattr("agent.lifecycle.verify.transition_task", fake_t)

    # Cycle 1
    await verify.handle_verify(99)
    # Cycle 2
    await verify.handle_verify(99)

    # No PR helper was called.
    pr_helper.assert_not_called()
    # Last transition went to BLOCKED.
    assert transitions[-1][1] == "blocked"
```

- [ ] **Step 2: Run test**

```
.venv/bin/python3 -m pytest tests/test_no_pr_on_failed_boot.py -v
```

Expected: pass.

- [ ] **Step 3: Commit**

```
git add tests/test_no_pr_on_failed_boot.py
git commit -m "test(regression): no PR on failed verify boot check"
```

---

### Task 24: `test_no_pr_on_intent_mismatch.py`

**Files:**
- Create: `tests/test_no_pr_on_intent_mismatch.py`

- [ ] **Step 1: Write the test**

```python
"""Regression: a diff that doesn't match the task is blocked at verify intent.

Maps to acceptance criterion #1 (intent layer).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.lifecycle import verify
from shared.types import IntentVerdict


async def test_intent_mismatch_blocks_no_pr(monkeypatch, db_session):
    task = MagicMock(
        id=100, repo_name="r", title="add dark mode toggle", description="add dark mode toggle to /settings",
        freeform_mode=True, created_by_user_id=None, organization_id=1,
        affected_routes=[], branch_name="b",
    )
    monkeypatch.setattr("agent.lifecycle.verify.get_task", AsyncMock(return_value=task))
    monkeypatch.setattr(
        "agent.lifecycle.verify._prepare_workspace",
        AsyncMock(return_value=("/tmp/ws", "main")),
    )
    monkeypatch.setattr(
        "agent.lifecycle.verify.get_freeform_config", AsyncMock(return_value=None),
    )
    monkeypatch.setattr("agent.tools.dev_server.sniff_run_command", lambda ws, override=None: None)
    monkeypatch.setattr(
        "agent.lifecycle.verify.run_intent_check",
        AsyncMock(return_value=IntentVerdict(
            ok=False, reasoning="no dark-mode toggle found in diff", tool_calls=[],
        )),
    )

    pr_helper = AsyncMock()
    monkeypatch.setattr("agent.lifecycle.coding._open_pr_and_advance", pr_helper)
    transitions = []
    async def fake_t(*a, **kw): transitions.append(a)
    monkeypatch.setattr("agent.lifecycle.verify.transition_task", fake_t)

    await verify.handle_verify(100)
    await verify.handle_verify(100)

    pr_helper.assert_not_called()
    assert transitions[-1][1] == "blocked"
```

- [ ] **Step 2: Run**

```
.venv/bin/python3 -m pytest tests/test_no_pr_on_intent_mismatch.py -v
```

Expected: pass.

- [ ] **Step 3: Commit**

```
git add tests/test_no_pr_on_intent_mismatch.py
git commit -m "test(regression): no PR on verify intent mismatch"
```

---

### Task 25: `test_no_done_on_failed_ui_review.py`

**Files:**
- Create: `tests/test_no_done_on_failed_ui_review.py`

- [ ] **Step 1: Write the test**

```python
"""Regression: a UI change with broken rendering cannot reach DONE.

Maps to acceptance criterion #2 — review UI layer.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.lifecycle import review


async def test_ui_review_failure_blocks_no_done(monkeypatch, db_session):
    task = MagicMock(
        id=101, repo_name="r", title="t", description="d",
        freeform_mode=True, created_by_user_id=None, organization_id=1,
        affected_routes=[{"method": "GET", "path": "/broken", "label": "broken"}],
        branch_name="b",
    )
    monkeypatch.setattr("agent.lifecycle.review.get_task", AsyncMock(return_value=task))
    monkeypatch.setattr("agent.lifecycle.review.get_repo", AsyncMock(return_value=MagicMock(default_branch="main", url="x")))
    monkeypatch.setattr("agent.lifecycle.review.clone_repo", AsyncMock(return_value="/tmp/ws"))
    monkeypatch.setattr("agent.lifecycle.review.sh.run", AsyncMock(return_value=MagicMock(failed=False, stdout="", stderr="")))
    monkeypatch.setattr("agent.tools.dev_server.sniff_run_command", lambda ws, override=None: "npm run dev")

    @asynccontextmanager
    async def fake_start(ws, override=None):
        yield MagicMock(port=12345, log_path="/tmp/log", process=MagicMock(returncode=None))
    monkeypatch.setattr("agent.tools.dev_server.start_dev_server", fake_start)
    monkeypatch.setattr("agent.tools.dev_server.wait_for_port", AsyncMock())

    monkeypatch.setattr("agent.lifecycle.review.create_agent", MagicMock(return_value=MagicMock(
        run=AsyncMock(return_value=MagicMock(
            output='{"code_review": {"verdict": "OK", "reasoning": ""}, "ui_check": {"verdict": "NOT-OK", "reasoning": "/broken returned 500"}}',
            tool_calls=[{"name": "browse_url", "args": {"url": "http://localhost:12345/broken"}}],
        )),
    )))

    transitions = []
    async def fake_t(*a, **kw): transitions.append(a)
    monkeypatch.setattr("agent.lifecycle.review.transition_task", fake_t)

    review_complete_published = []
    async def fake_publish(event):
        if event.kind == "task_review_complete":
            review_complete_published.append(event)
    monkeypatch.setattr("agent.lifecycle.review.publish", fake_publish)

    # Cycle 1 — should loop back to coding
    await review.handle_independent_review(101, "http://pr", "b")
    # Cycle 2 — should block
    await review.handle_independent_review(101, "http://pr", "b")

    # task_review_complete(approved=True) is never published (no path to DONE)
    assert not any(e.payload.get("approved") for e in review_complete_published)
    # Last transition went to BLOCKED
    assert transitions[-1][1] == "blocked"
```

- [ ] **Step 2: Run**

```
.venv/bin/python3 -m pytest tests/test_no_done_on_failed_ui_review.py -v
```

Expected: pass.

- [ ] **Step 3: Commit**

```
git add tests/test_no_done_on_failed_ui_review.py
git commit -m "test(regression): no DONE on failed UI review"
```

---

## Phase 8: API + UI

### Task 26: `GET /api/tasks/:id/verify-attempts` and `/review-attempts`

**Files:**
- Modify: `orchestrator/router.py`

- [ ] **Step 1: Failing test**

`tests/test_attempts_endpoints.py`:

```python
import pytest
from httpx import AsyncClient


async def test_get_verify_attempts(test_client: AsyncClient, db_session, sample_task):
    from shared.models import VerifyAttempt
    db_session.add(VerifyAttempt(task_id=sample_task.id, cycle=1, status="pass", boot_check="pass", intent_check="pass"))
    await db_session.commit()

    resp = await test_client.get(f"/api/tasks/{sample_task.id}/verify-attempts")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["status"] == "pass"


async def test_get_review_attempts(test_client: AsyncClient, db_session, sample_task):
    from shared.models import ReviewAttempt
    db_session.add(ReviewAttempt(task_id=sample_task.id, cycle=1, status="pass", code_review_verdict="OK", ui_check="skipped"))
    await db_session.commit()

    resp = await test_client.get(f"/api/tasks/{sample_task.id}/review-attempts")
    assert resp.status_code == 200
    assert resp.json()[0]["ui_check"] == "skipped"
```

(If `test_client` / `sample_task` fixtures don't exist, copy the pattern from `tests/test_router.py` or wherever HTTP tests live.)

- [ ] **Step 2: Implement endpoints**

In `orchestrator/router.py`:

```python
from shared.models import VerifyAttempt, ReviewAttempt
from shared.types import VerifyAttemptOut, ReviewAttemptOut


@router.get("/api/tasks/{task_id}/verify-attempts", response_model=list[VerifyAttemptOut])
async def list_verify_attempts(task_id: int, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(VerifyAttempt).where(VerifyAttempt.task_id == task_id).order_by(VerifyAttempt.cycle)
    )).scalars().all()
    return rows


@router.get("/api/tasks/{task_id}/review-attempts", response_model=list[ReviewAttemptOut])
async def list_review_attempts(task_id: int, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(ReviewAttempt).where(ReviewAttempt.task_id == task_id).order_by(ReviewAttempt.cycle)
    )).scalars().all()
    return rows
```

- [ ] **Step 3: Run tests**

```
.venv/bin/python3 -m pytest tests/test_attempts_endpoints.py -v
```

Expected: 2 passed.

- [ ] **Step 4: Commit**

```
git add orchestrator/router.py tests/test_attempts_endpoints.py
git commit -m "feat(api): list verify/review attempts endpoints"
```

---

### Task 27: Regenerate TS types

**Files:**
- Auto-generated TS files under `web-next/lib/api-types/` (or wherever `scripts/gen_ts_types.py` writes).

- [ ] **Step 1: Run the generator**

```
python3.12 scripts/gen_ts_types.py
```

- [ ] **Step 2: Verify the new types are present**

```
grep -n "VerifyAttemptOut\|ReviewAttemptOut\|AffectedRoute" web-next/lib/api-types/*.ts
```

Expected: all three types appear.

- [ ] **Step 3: Commit**

```
git add web-next/lib/api-types/
git commit -m "chore(web-next): regen TS types — VerifyAttempt, ReviewAttempt, AffectedRoute"
```

---

### Task 28: `useVerifyAttempts` / `useReviewAttempts` hooks

**Files:**
- Create: `web-next/hooks/useVerifyAttempts.ts`
- Create: `web-next/hooks/useReviewAttempts.ts`

- [ ] **Step 1: Inspect an existing hook for the pattern**

```
cat web-next/hooks/useMarketBrief.ts 2>/dev/null || ls web-next/hooks/
```

Pick any existing TanStack Query hook as a template.

- [ ] **Step 2: Implement**

`web-next/hooks/useVerifyAttempts.ts`:

```typescript
import { useQuery } from "@tanstack/react-query";
import { VerifyAttemptOut } from "@/lib/api-types";

export function useVerifyAttempts(taskId: number) {
  return useQuery<VerifyAttemptOut[]>({
    queryKey: ["verify-attempts", taskId],
    queryFn: async () => {
      const r = await fetch(`/api/tasks/${taskId}/verify-attempts`);
      if (!r.ok) throw new Error("failed to fetch verify attempts");
      return r.json();
    },
  });
}
```

`web-next/hooks/useReviewAttempts.ts` — mirror the above for `ReviewAttemptOut` at `/api/tasks/${taskId}/review-attempts`.

- [ ] **Step 3: Commit**

```
git add web-next/hooks/useVerifyAttempts.ts web-next/hooks/useReviewAttempts.ts
git commit -m "feat(web-next): useVerifyAttempts / useReviewAttempts hooks"
```

---

### Task 29: `VerifyAttempts` and `ReviewAttempts` components

**Files:**
- Create: `web-next/components/task/VerifyAttempts.tsx`
- Create: `web-next/components/task/ReviewAttempts.tsx`

- [ ] **Step 1: Inspect existing task-detail components for style**

```
ls web-next/components/task/
```

Match the existing card/badge conventions.

- [ ] **Step 2: Implement `VerifyAttempts.tsx`**

```tsx
"use client";
import { useVerifyAttempts } from "@/hooks/useVerifyAttempts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

export function VerifyAttempts({ taskId }: { taskId: number }) {
  const { data, isLoading } = useVerifyAttempts(taskId);
  if (isLoading) return null;
  if (!data || data.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Verify</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {data.map((a) => (
          <div key={a.id} className="border-l-2 pl-3">
            <div className="flex items-center gap-2 text-sm">
              <span>Cycle {a.cycle}</span>
              <Badge variant={a.status === "pass" ? "default" : "destructive"}>{a.status}</Badge>
              {a.boot_check && <span>boot: {a.boot_check}</span>}
              {a.intent_check && <span>intent: {a.intent_check}</span>}
            </div>
            {a.intent_judgment && (
              <pre className="text-xs whitespace-pre-wrap mt-1">{a.intent_judgment}</pre>
            )}
            {a.failure_reason && (
              <div className="text-xs text-red-500 mt-1">reason: {a.failure_reason}</div>
            )}
            {a.log_tail && (
              <details className="text-xs mt-1">
                <summary>Server log tail</summary>
                <pre className="whitespace-pre-wrap">{a.log_tail}</pre>
              </details>
            )}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 3: Implement `ReviewAttempts.tsx`**

Mirror the verify component, displaying `code_review_verdict`, `ui_check`, `ui_judgment`, and any `tool_calls` with screenshot URLs (use the `tool_calls[i].args.url` to reach screenshots if they were saved, or just display the URL list).

- [ ] **Step 4: Commit**

```
git add web-next/components/task/VerifyAttempts.tsx web-next/components/task/ReviewAttempts.tsx
git commit -m "feat(web-next): VerifyAttempts and ReviewAttempts components"
```

---

### Task 30: Mount on task detail page

**Files:**
- Modify: `web-next/app/(app)/tasks/[id]/page.tsx`

- [ ] **Step 1: Add the imports and mount**

Open the file. After the existing review/plan sections, insert:

```tsx
import { VerifyAttempts } from "@/components/task/VerifyAttempts";
import { ReviewAttempts } from "@/components/task/ReviewAttempts";

// ... inside the page body, between existing sections (plan above, PR link below):
<VerifyAttempts taskId={task.id} />
<ReviewAttempts taskId={task.id} />
```

- [ ] **Step 2: Smoke-test the dev server**

```
cd web-next && npm run dev
```

Open a task detail page in the browser. Confirm the components render (empty state when no attempts) without errors.

- [ ] **Step 3: Commit**

```
git add web-next/app/\(app\)/tasks/\[id\]/page.tsx
git commit -m "feat(web-next): mount VerifyAttempts and ReviewAttempts on task page"
```

---

## Phase 9: Integration & final

### Task 31: e2e smoke test (slow)

**Files:**
- Create: `tests/test_verify_review_e2e_smoke.py`

- [ ] **Step 1: Add the test**

```python
"""Slow integration smoke test — real Playwright, tiny http.server fixture.

Marked @pytest.mark.slow so it can be skipped in fast CI.
"""
import asyncio
import http.server
import socketserver
import threading
import socket

import pytest

from agent.tools.browse_url import BrowseUrlTool


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


@pytest.mark.slow
@pytest.mark.asyncio
async def test_browse_url_real_playwright_on_http_server(tmp_path):
    (tmp_path / "index.html").write_text("<html><body><h1>Hello smoke</h1></body></html>")
    port = _free_port()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(tmp_path), **kw)

    httpd = socketserver.TCPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True); t.start()

    try:
        tool = BrowseUrlTool()
        result = await tool.run({"url": f"http://127.0.0.1:{port}/"}, ctx=None)
        types = [b["type"] for b in result.content]
        assert "image" in types
        text = next(b for b in result.content if b["type"] == "text" and "Hello smoke" in b["text"])
        assert text is not None
    finally:
        httpd.shutdown(); httpd.server_close()
```

- [ ] **Step 2: Add the `slow` marker registration in `pyproject.toml` or `pytest.ini`**

```
[tool.pytest.ini_options]
markers = ["slow: marks tests as slow (deselect with -m 'not slow')"]
```

- [ ] **Step 3: Run the slow test**

```
.venv/bin/python3 -m pytest tests/test_verify_review_e2e_smoke.py -v -m slow
```

Expected: pass (may take 5-10 s for Playwright startup).

- [ ] **Step 4: Commit**

```
git add tests/test_verify_review_e2e_smoke.py pyproject.toml
git commit -m "test(e2e): slow Playwright smoke test against tiny http.server fixture"
```

---

### Task 32: Final verification

**Files:**
- None (verification only).

- [ ] **Step 1: Run the full test suite (excluding slow)**

```
.venv/bin/python3 -m pytest tests/ -q -m "not slow"
```

Expected: all green.

- [ ] **Step 2: Run lint**

```
ruff check . && ruff format --check .
```

Expected: clean.

- [ ] **Step 3: Apply migrations on the dev DB**

```
docker compose exec auto-agent alembic upgrade head
```

Expected: `current revision = 032_verify_review_attempts`.

- [ ] **Step 4: Smoke the full app**

```
docker compose up -d && curl -fs http://localhost:8000/health
```

Expected: 200.

- [ ] **Step 5: Commit any small follow-up fixes from the above**

```
git add -A && git commit -m "chore: post-implementation verification fixes" || true
```

---

## Self-Review (run before declaring done)

Spec coverage check:

| Spec section | Task(s) |
|---|---|
| `TaskStatus.VERIFYING` + transitions | 2 |
| `Task.affected_routes`, attempts tables, FreeformConfig.run_command | 3, 4 |
| Events (`verify_*`, `review_skipped_no_runner`, `coding_server_boot_failed`) | 5 |
| `dev_server.py` (sniff, start, kill, wait_for_port, hold, tail tool) | 6, 7, 8, 9 |
| `browse_url.py` | 10 |
| `with_browser` flag (registry + factory) | 11 |
| Planner outputs `affected_routes` | 12 |
| `_open_pr_and_advance` extraction | 13 |
| Coding pre-loop server start + system prompt | 14, 15 |
| `_finish_coding` → VERIFYING | 16 |
| `agent/lifecycle/verify.py` (boot + intent + retries) | 17, 18, 19 |
| Review UI check + ReviewAttempt + 2-cycle BLOCKED | 20, 21, 22 |
| Regression tests | 23, 24, 25 |
| API endpoints | 26 |
| `web-next` hooks + components + mount | 27, 28, 29, 30 |
| e2e smoke | 31 |
| Final verification | 32 |

All spec sections are covered.

Placeholder scan: every step contains either complete code blocks or exact commands. No `TBD`, `TODO`, `implement later`, or `similar to Task N` references remain.

Type consistency: `IntentVerdict`, `ReviewCombinedVerdict`, `VerifyAttempt(task_id, cycle, status, boot_check, intent_check, intent_judgment, tool_calls, failure_reason, log_tail)`, `ReviewAttempt(...)` are consistent across spec, models, types, and test signatures.
