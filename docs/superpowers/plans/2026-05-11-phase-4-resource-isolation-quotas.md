# Phase 4 — Resource isolation + quotas

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop any one tenant from degrading the others. Each org has a `Plan` with hard caps (concurrent tasks, daily task count, daily LLM tokens). Workspace directories, queue dispatch, LLM usage accounting, and rate-limit enforcement all become per-org. Tasks that exhaust their org's LLM budget transition to a new `BLOCKED_ON_QUOTA` state.

**Architecture:** Three new tables (`plans`, `usage_events`, plus `organizations.plan_id`) introduced in migration 029. The existing global+per-repo dispatcher in `orchestrator/queue.py` grows a per-org cap and a per-org daily-creation counter; quota state lives in Postgres (no Redis dependency in v1). LLM providers don't change — instead, the agent loop emits one `usage_events` row per `complete()` call, using a price-per-million-tokens table in a new `shared/pricing.py`. Settings UI gains a `/settings/usage` page with current-period totals and plan card.

**Tech Stack:** Python 3.12, SQLAlchemy async, Alembic, FastAPI, Pydantic v2, structlog, Next.js (App Router) + TanStack Query.

---

## Pre-flight (read before starting)

- **Branch off `feat/phase-3-per-org-integrations`** (the current branch). Phase 3 is on PR #40, not yet merged. Suggested branch: `feat/phase-4-quotas`. Reason: Phase 4 depends on Phase 3's per-org config plumbing (org-scoped tasks, `current_org_id_dep`, the org sidebar nav). If Phase 3 lands on main during this work, rebase onto main.
- **No deploy-time secrets required.** Plans table is seeded by the migration; no new env vars.
- **The cumulative `usage_events` table can grow fast.** Acceptable for v1 — index by `(org_id, occurred_at DESC)`. A retention/aggregation job is out of scope (Phase 6).
- **`BLOCKED_ON_AUTH` is not currently in `TRANSITIONS`** even though it exists as a `TaskStatus`. It's set directly via `transition()` from non-state-machine contexts in `run.py` and the state machine accepts it implicitly because the `allowed` lookup defaults to `set()` when the *target* state isn't in TRANSITIONS keys — but the *source* state being missing from TRANSITIONS means there's no exit transition defined. We must add `BLOCKED_ON_QUOTA` to the state machine in both directions (entry from any active state, exit back to QUEUED on midnight rollover or admin unblock) and, while we're there, also wire up `BLOCKED_ON_AUTH` exits explicitly.

### Testing conventions

The existing suite is **mock-heavy** — most tests use `AsyncMock` on `session.execute` (see `tests/test_queue_multi_tenant.py` for the canonical pattern). There is no `session`/`client`/`login_as` fixture in `tests/conftest.py` and no `tests/helpers.py`. The testcontainers-backed real-DB suite is deferred (carried forward from Phase 2 handover).

Phase 4's quota math touches multiple tables in a single test (orgs, plans, tasks, usage_events). Mocking that with `AsyncMock` gets tangled fast. **Task A0 below creates a real-DB `session` fixture** that wraps each test in a transaction that rolls back at end-of-test. The fixture is `pytest.skip`-ped when `DATABASE_URL` is not set, so the local mock-based suite still runs without Postgres; CI and `docker compose exec auto-agent pytest ...` exercise the full set.

HTTP tests follow the existing pattern from `tests/test_slack_oauth.py`: build a `FastAPI()` app per test, include the router, override `current_org_id_dep` via `app.dependency_overrides`. No bearer-token JWT minting needed.

---

## Glossary

| Term | Meaning |
|---|---|
| **Plan** | Tier-level limits attached to an org (`free`, `pro`, `team`). One row in `plans`. |
| **`max_concurrent_tasks`** | Per-org cap on tasks in any `ACTIVE_STATUSES` state simultaneously. |
| **`max_tasks_per_day`** | Per-org cap on rows created in `tasks` for an org in a calendar day (UTC). Enforced at task-create endpoints; returns HTTP 429. |
| **`max_input_tokens_per_day` / `max_output_tokens_per_day`** | Per-org daily LLM token caps. Counted from `usage_events` rows for the current UTC day. When exceeded, in-flight tasks transition to `BLOCKED_ON_QUOTA`. |
| **`BLOCKED_ON_QUOTA`** | New `TaskStatus`. Set when an LLM call would push the org over its daily token cap. Mirrors `BLOCKED_ON_AUTH`: paused, does NOT occupy a concurrency slot. |
| **`usage_events`** | Append-only fact table. One row per LLM call (`kind="llm_call"`). Other kinds reserved for future use. |
| **Cost cents** | Estimated cost in cents (NUMERIC(10,4) — supports fractions) for an LLM call, computed from `shared/pricing.py`'s price-per-million-tokens table. Estimate only; not a billing source of truth (Phase 5 owns billing). |

---

## File Structure

### New files

| Path | Responsibility |
|---|---|
| `migrations/versions/029_plans_and_usage.py` | Creates `plans` + `usage_events`, adds `organizations.plan_id` (FK to `plans`, NOT NULL after backfill), seeds three plan rows. |
| `shared/pricing.py` | Static `PRICE_PER_MILLION_TOKENS` map keyed by friendly model name. `estimate_cost_cents(model, input_tokens, output_tokens) -> Decimal`. |
| `shared/quotas.py` | All quota lookups in one place: `get_plan_for_org`, `count_active_tasks_for_org`, `count_tasks_created_today`, `sum_tokens_today`, `would_exceed_token_cap`. Pure functions over an `AsyncSession`. |
| `orchestrator/usage.py` | `emit_usage_event(org_id, task_id, model, usage)` — writes one `usage_events` row. Awaitable, runs in the same session as the call site when supplied, else opens its own. |
| `tests/helpers.py` | `make_org_and_task` and a `_ensure_default_plan` seeder; reused by every DB-touching test. |
| `tests/test_models_phase4.py` | ORM introspection: Plan/UsageEvent columns, Organization.plan_id FK, BLOCKED_ON_QUOTA enum. |
| `tests/test_state_machine.py` | TRANSITIONS dict shape — BLOCKED_ON_QUOTA entry/exit + BLOCKED_ON_AUTH exit. |
| `tests/test_migration_029.py` | `alembic upgrade 029` then `downgrade 028` round-trip; verify three seeded plans; verify NOT NULL on `organizations.plan_id`. |
| `tests/test_pricing.py` | Cost-cents math for known models; unknown model falls back to a documented default; zero tokens → zero cost. |
| `tests/test_quotas.py` | Each function in `shared/quotas.py` exercised: empty org returns zeros; rows in another org are excluded; only-today rows are counted. |
| `tests/test_usage_event_emission.py` | A fake provider call wired through the agent loop emits exactly one `usage_events` row with the right `org_id`, `task_id`, `model`, and token counts. |
| `tests/test_workspace_per_org_dirs.py` | `clone_repo(org_id=42, task_id=7)` writes to `<root>/42/task-7`; `cleanup_workspace(task_id=7, org_id=42)` removes the per-org subtree only. |
| `tests/test_queue_per_org_cap.py` | Org A at its plan's `max_concurrent_tasks` cap → its next QUEUED task is skipped, org B's QUEUED task is started. |
| `tests/test_rate_limit_task_create.py` | After `max_tasks_per_day` POSTs to `/api/tasks`, the next POST returns 429 with `Retry-After` header. Counter resets at UTC midnight. |
| `tests/test_blocked_on_quota.py` | Pre-LLM gate transitions an in-flight task to `BLOCKED_ON_QUOTA` when org's daily input-token sum + next call's estimate would exceed the cap. |
| `tests/test_usage_endpoint.py` | `GET /api/usage/summary` returns this-period totals + the plan caps, scoped to caller's `current_org_id`. |
| `web-next/lib/usage.ts` | Typed fetch helpers + `UsageSummary` interface for `/api/usage/summary`. |
| `web-next/hooks/useUsage.ts` | `useUsageSummary()` TanStack Query hook (60s refetch). |
| `web-next/app/(app)/settings/usage/page.tsx` | Usage page: plan card + four "X / cap" bars (concurrent, tasks today, input tokens today, output tokens today). |
| `docs/superpowers/plans/2026-05-11-handover-after-phase-4.md` | New handover at end of execution. |

### Modified files

| Path | What changes |
|---|---|
| `shared/models.py` | Add `Plan` ORM class (after `Organization`); add `plan_id` column + `plan` relationship to `Organization`; add `UsageEvent` ORM class; add `TaskStatus.BLOCKED_ON_QUOTA = "blocked_on_quota"`. |
| `orchestrator/state_machine.py` | Add `BLOCKED_ON_QUOTA` to `TRANSITIONS` as both a target (from PLANNING/CODING/BLOCKED_ON_AUTH) and a source (→ QUEUED on unblock); add `BLOCKED_ON_AUTH` as a source row (→ QUEUED, PLANNING, CODING). |
| `orchestrator/queue.py` | Keep `BLOCKED_ON_QUOTA` out of `ACTIVE_STATUSES`. New helper `_org_at_concurrency_cap(session, org_id, cap) -> bool`. `next_eligible_task` consults the per-org cap (read from `org.plan.max_concurrent_tasks`) and skips capped orgs head-of-line. `can_start_task` calls the new helper too. |
| `orchestrator/router.py` | `POST /api/tasks` (and `_check_rate_limit` site) calls `quotas.enforce_task_create(session, org_id)` which raises HTTPException(429) when over `max_tasks_per_day`. Add `GET /api/usage/summary`. |
| `agent/workspace.py` | `clone_repo` and `cleanup_workspace` accept `organization_id: int | None` and write under `<WORKSPACES_DIR>/<org_id>/task-<task_id>` when set. Default (org_id=None) preserves legacy `<WORKSPACES_DIR>/task-<task_id>` for tests and back-compat. |
| `agent/lifecycle/coding.py`, `agent/lifecycle/planning.py`, `agent/lifecycle/cleanup.py` | Thread `organization_id` (already on `Task`) into the existing `clone_repo` / `cleanup_workspace` calls. |
| `agent/loop.py` | Accept `org_id: int | None = None`, `task_id: int | None = None`, `usage_sink: UsageSink | None = None` kwargs. After every `provider.complete()` call, if `usage_sink` is set and `org_id`/`task_id` known, await `usage_sink.emit(model=self._provider.model, usage=response.usage)`. Add a *pre-call* gate: `if usage_sink and usage_sink.would_exceed_token_cap(): raise QuotaExceeded(...)`. |
| `agent/main.py` (and any other call sites that construct `AgentLoop`) | Pass `org_id=task.organization_id`, `task_id=task.id`, and a `UsageSink` initialised with those. On `QuotaExceeded`, transition the task to `BLOCKED_ON_QUOTA` and exit cleanly (no FAILED). |
| `shared/types.py` | Add `UsageSummary` Pydantic model + `PlanRead` for the `/api/usage/summary` response. |
| `web-next/components/sidebar/sidebar.tsx` (or wherever the settings sub-nav lives — mirror Phase 3) | Add "Usage" link under Settings. |
| `web-next/app/(app)/settings/layout.tsx` | Add `Usage` to the nav list (mirrors "Organization" and "Integrations"). |
| `tests/conftest.py` | Add real-DB `session` fixture (skip-if no `DATABASE_URL`). |
| `tests/test_org_scoping_coverage.py` | `/api/usage/summary` uses `current_org_id_dep`, so it should pass automatically — verify in the test. No allowlist change. |

---

## Track A — Migration 029 + ORM models

The migration is load-bearing — pause everything else until 029 lands and the three new ORM models compile against it.

### Task A0: Add `session` fixture + `tests/helpers.py`

These are referenced by most later tests. Doing them first means each subsequent task can drop straight into TDD.

**Files:**
- Modify: `tests/conftest.py`
- Create: `tests/helpers.py`

- [ ] **Step 1: Append the `session` fixture to `tests/conftest.py`**

```python
import os

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def session():
    """Real-DB session that rolls back at end of test.

    Requires DATABASE_URL pointing at a writable Postgres (CI + docker compose).
    Skips locally if unset so the mock-based suite still passes standalone.
    """
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set — Phase 4 DB tests need real Postgres")

    from shared.database import async_session

    async with async_session() as s:
        # Use an outer transaction; roll back on exit.
        async with s.begin():
            yield s
            await s.rollback()
```

- [ ] **Step 2: Create `tests/helpers.py`**

```python
"""Test helpers for Phase 4 quota tests.

Keep this file tiny — only seed-and-return helpers that are reused across
≥ 2 test files. One-off seeding lives in the test itself.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Organization, Plan, Task, TaskStatus


async def _ensure_default_plan(session: AsyncSession) -> Plan:
    from sqlalchemy import select
    q = await session.execute(select(Plan).where(Plan.name == "free"))
    plan = q.scalar_one_or_none()
    if plan is not None:
        return plan
    plan = Plan(
        name="free",
        max_concurrent_tasks=1,
        max_tasks_per_day=5,
        max_input_tokens_per_day=1_000_000,
        max_output_tokens_per_day=250_000,
        max_members=3,
        monthly_price_cents=0,
    )
    session.add(plan)
    await session.flush()
    return plan


async def make_org_and_task(
    session: AsyncSession,
    *,
    status: TaskStatus = TaskStatus.QUEUED,
    slug: str = "test-org",
) -> tuple[Organization, Task]:
    plan = await _ensure_default_plan(session)
    org = Organization(name="Test Org", slug=slug, plan_id=plan.id)
    session.add(org)
    await session.flush()
    task = Task(
        title="t",
        description="",
        source="manual",
        source_id=f"src-{org.id}",
        status=status,
        organization_id=org.id,
    )
    session.add(task)
    await session.flush()
    return org, task
```

- [ ] **Step 3: Smoke-test the fixture**

```
docker compose up -d postgres
DATABASE_URL="postgresql+asyncpg://autoagent:changeme@localhost:5432/autoagent" \
  .venv/bin/python3 -c "import pytest_asyncio; print('ok')"
```

(The fixture is exercised by every Phase 4 DB test below; no dedicated unit test needed.)

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py tests/helpers.py
git commit -m "test: add real-DB session fixture + helpers for Phase 4"
```

### Task A1: Create migration 029 (schema + seed + plan_id NOT NULL)

**Files:**
- Create: `migrations/versions/029_plans_and_usage.py`
- Test: indirect (Task A4 round-trips upgrade/downgrade)

- [ ] **Step 1: Write the migration**

```python
"""029 — plans + per-org plan_id + usage_events

Adds plan tiers, attaches each organization to a plan (default 'free'),
and a usage_events fact table for per-call LLM accounting.

Seeded plan rows are intentionally hardcoded — Phase 4 ships with three
tiers (free/pro/team). Phase 5 may add columns or rows; do not remove
the seeded rows in any future migration without an explicit data plan.

Revision ID: 029
Revises: 028
Create Date: 2026-05-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "029"
down_revision: Union[str, None] = "028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    plans = op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("max_concurrent_tasks", sa.Integer(), nullable=False),
        sa.Column("max_tasks_per_day", sa.Integer(), nullable=False),
        sa.Column("max_input_tokens_per_day", sa.BigInteger(), nullable=False),
        sa.Column("max_output_tokens_per_day", sa.BigInteger(), nullable=False),
        sa.Column("max_members", sa.Integer(), nullable=False),
        sa.Column("monthly_price_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("name", name="uq_plans_name"),
    )

    op.bulk_insert(
        plans,
        [
            {"name": "free", "max_concurrent_tasks": 1, "max_tasks_per_day": 5,
             "max_input_tokens_per_day": 1_000_000, "max_output_tokens_per_day": 250_000,
             "max_members": 3, "monthly_price_cents": 0},
            {"name": "pro", "max_concurrent_tasks": 3, "max_tasks_per_day": 50,
             "max_input_tokens_per_day": 10_000_000, "max_output_tokens_per_day": 2_500_000,
             "max_members": 5, "monthly_price_cents": 0},
            {"name": "team", "max_concurrent_tasks": 5, "max_tasks_per_day": 200,
             "max_input_tokens_per_day": 50_000_000, "max_output_tokens_per_day": 12_500_000,
             "max_members": 25, "monthly_price_cents": 0},
        ],
    )

    op.add_column(
        "organizations",
        sa.Column("plan_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_organizations_plan_id",
        "organizations",
        "plans",
        ["plan_id"],
        ["id"],
    )
    op.execute(
        "UPDATE organizations SET plan_id = (SELECT id FROM plans WHERE name = 'free')"
    )
    op.alter_column("organizations", "plan_id", nullable=False)

    op.create_table(
        "usage_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_cents", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_usage_events_org_time",
        "usage_events",
        ["org_id", sa.text("occurred_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_events_org_time", "usage_events")
    op.drop_table("usage_events")
    op.drop_constraint("fk_organizations_plan_id", "organizations", type_="foreignkey")
    op.drop_column("organizations", "plan_id")
    op.drop_table("plans")
```

- [ ] **Step 2: Commit**

```bash
git add migrations/versions/029_plans_and_usage.py
git commit -m "feat(migrations): add 029 — plans + per-org plan_id + usage_events"
```

### Task A2: Add `Plan` + `UsageEvent` ORM models, attach `plan_id` to Organization

**Files:**
- Modify: `shared/models.py`
- Test: `tests/test_models_phase4.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models_phase4.py
"""Phase 4 ORM models — column / FK introspection."""
from __future__ import annotations

from shared.models import Organization, Plan, UsageEvent, TaskStatus


def test_plan_columns() -> None:
    cols = {c.name for c in Plan.__table__.columns}
    assert cols == {
        "id", "name", "max_concurrent_tasks", "max_tasks_per_day",
        "max_input_tokens_per_day", "max_output_tokens_per_day",
        "max_members", "monthly_price_cents",
    }


def test_organization_has_plan_fk() -> None:
    org_cols = {c.name for c in Organization.__table__.columns}
    assert "plan_id" in org_cols
    # plan relationship attribute is accessible
    assert hasattr(Organization, "plan")


def test_usage_event_columns() -> None:
    cols = {c.name for c in UsageEvent.__table__.columns}
    assert cols == {
        "id", "org_id", "task_id", "kind", "model",
        "input_tokens", "output_tokens", "cost_cents", "occurred_at",
    }


def test_blocked_on_quota_enum_value_exists() -> None:
    assert TaskStatus.BLOCKED_ON_QUOTA.value == "blocked_on_quota"
```

- [ ] **Step 2: Run test to verify it fails**

```
.venv/bin/python3 -m pytest tests/test_models_phase4.py -v
```
Expected: ImportError for `Plan` / `UsageEvent`, or `AttributeError: BLOCKED_ON_QUOTA`.

- [ ] **Step 3: Add the new models + enum value**

Insert after the existing `Organization` class definition in `shared/models.py`:

```python
class Plan(Base):
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), nullable=False, unique=True)
    max_concurrent_tasks = Column(Integer, nullable=False)
    max_tasks_per_day = Column(Integer, nullable=False)
    max_input_tokens_per_day = Column(BigInteger, nullable=False)
    max_output_tokens_per_day = Column(BigInteger, nullable=False)
    max_members = Column(Integer, nullable=False)
    monthly_price_cents = Column(Integer, nullable=False, default=0)
```

Add a `plan_id` column + `plan` relationship to the existing `Organization` class:

```python
class Organization(Base):
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)

    plan = relationship("Plan", lazy="joined")
```

Add `UsageEvent` near the other fact tables (after `TaskMessage`):

```python
class UsageEvent(Base):
    __tablename__ = "usage_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    org_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    kind = Column(String(32), nullable=False)
    model = Column(String(64), nullable=True)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cost_cents = Column(Numeric(10, 4), nullable=False, default=0)
    occurred_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
```

Add to `TaskStatus` enum (after `BLOCKED_ON_AUTH`):

```python
class TaskStatus(str, enum.Enum):
    # ...
    BLOCKED_ON_AUTH = "blocked_on_auth"
    BLOCKED_ON_QUOTA = "blocked_on_quota"
    BLOCKED = "blocked"
    FAILED = "failed"
```

If `BigInteger` / `Numeric` / `ForeignKey` / `relationship` aren't imported at the top of the file, add them. (`shared/models.py` already imports `Column`, `Integer`, `String`, `DateTime`, etc. — append the missing ones.)

- [ ] **Step 4: Run test to verify it passes**

```
.venv/bin/python3 -m pytest tests/test_models_phase4.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add shared/models.py tests/test_models_phase4.py
git commit -m "feat(models): add Plan, UsageEvent, BLOCKED_ON_QUOTA"
```

### Task A3: Wire `BLOCKED_ON_QUOTA` into the state machine

**Files:**
- Modify: `orchestrator/state_machine.py`
- Create: `tests/test_state_machine.py` (file does not exist yet)

- [ ] **Step 1: Write the failing test**

Create `tests/test_state_machine.py`:

```python
"""State machine transitions — Phase 4 additions for BLOCKED_ON_QUOTA + explicit BLOCKED_ON_AUTH exits."""

from shared.models import TaskStatus
from orchestrator.state_machine import TRANSITIONS


def test_blocked_on_quota_can_be_entered_from_active_states() -> None:
    for src in (TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.QUEUED):
        assert TaskStatus.BLOCKED_ON_QUOTA in TRANSITIONS[src], (
            f"{src.value} should be able to transition to BLOCKED_ON_QUOTA"
        )


def test_blocked_on_quota_exits_to_queued() -> None:
    allowed = TRANSITIONS[TaskStatus.BLOCKED_ON_QUOTA]
    assert TaskStatus.QUEUED in allowed
    assert TaskStatus.FAILED in allowed


def test_blocked_on_auth_exits_defined() -> None:
    # Phase 4 also defines BLOCKED_ON_AUTH exits (previously implicit).
    allowed = TRANSITIONS[TaskStatus.BLOCKED_ON_AUTH]
    assert TaskStatus.QUEUED in allowed
```

- [ ] **Step 2: Run test to verify it fails**

```
.venv/bin/python3 -m pytest tests/test_state_machine.py -v -k "blocked_on_quota or blocked_on_auth_exits"
```
Expected: KeyError on `BLOCKED_ON_QUOTA` and `BLOCKED_ON_AUTH`.

- [ ] **Step 3: Extend the TRANSITIONS dict**

In `orchestrator/state_machine.py`, edit the `TRANSITIONS` dict — add `BLOCKED_ON_QUOTA` to each active-state target set, and add new source rows for both `BLOCKED_ON_QUOTA` and `BLOCKED_ON_AUTH`:

```python
TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.INTAKE: {TaskStatus.CLASSIFYING},
    TaskStatus.CLASSIFYING: {TaskStatus.QUEUED, TaskStatus.FAILED},
    TaskStatus.QUEUED: {
        TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.DONE,
        TaskStatus.BLOCKED_ON_AUTH, TaskStatus.BLOCKED_ON_QUOTA,
    },
    TaskStatus.PLANNING: {
        TaskStatus.AWAITING_APPROVAL, TaskStatus.AWAITING_CLARIFICATION,
        TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.BLOCKED_ON_QUOTA,
    },
    TaskStatus.AWAITING_APPROVAL: {TaskStatus.CODING, TaskStatus.PLANNING},
    TaskStatus.AWAITING_CLARIFICATION: {TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.FAILED},
    TaskStatus.CODING: {
        TaskStatus.PR_CREATED, TaskStatus.AWAITING_CLARIFICATION,
        TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.DONE,
        TaskStatus.BLOCKED_ON_QUOTA,
    },
    TaskStatus.PR_CREATED: {TaskStatus.AWAITING_CI},
    TaskStatus.AWAITING_CI: {TaskStatus.AWAITING_REVIEW, TaskStatus.CODING, TaskStatus.FAILED},
    TaskStatus.AWAITING_REVIEW: {TaskStatus.DONE, TaskStatus.CODING},
    TaskStatus.BLOCKED: {TaskStatus.CODING, TaskStatus.PLANNING, TaskStatus.FAILED, TaskStatus.DONE},
    TaskStatus.BLOCKED_ON_AUTH: {TaskStatus.QUEUED, TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.FAILED},
    TaskStatus.BLOCKED_ON_QUOTA: {TaskStatus.QUEUED, TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.FAILED},
    TaskStatus.DONE: set(),
    TaskStatus.FAILED: {TaskStatus.DONE},
}
```

- [ ] **Step 4: Run tests to verify they pass**

```
.venv/bin/python3 -m pytest tests/test_state_machine.py -v
```
Expected: all green (existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/state_machine.py tests/test_state_machine.py
git commit -m "feat(state-machine): add BLOCKED_ON_QUOTA + define BLOCKED_ON_AUTH exits"
```

### Task A4: Round-trip Alembic upgrade/downgrade

**Files:**
- Create: `tests/test_migration_029.py`

- [ ] **Step 1: Write the test**

```python
# tests/test_migration_029.py
"""Round-trip alembic upgrade 029 / downgrade 028 against a temp Postgres."""
from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs a running Postgres (set DATABASE_URL)",
)


def _alembic_cfg() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2"))
    return cfg


def test_upgrade_to_029_creates_plans_and_seeds_three_rows() -> None:
    cfg = _alembic_cfg()
    command.upgrade(cfg, "029")

    engine = sa.create_engine(cfg.get_main_option("sqlalchemy.url"))
    with engine.begin() as conn:
        rows = conn.execute(sa.text("SELECT name FROM plans ORDER BY id")).fetchall()
        assert [r[0] for r in rows] == ["free", "pro", "team"]
        # organizations.plan_id is NOT NULL after backfill
        null_plans = conn.execute(
            sa.text("SELECT COUNT(*) FROM organizations WHERE plan_id IS NULL")
        ).scalar_one()
        assert null_plans == 0


def test_downgrade_from_029_to_028_drops_plans_and_column() -> None:
    cfg = _alembic_cfg()
    command.upgrade(cfg, "029")
    command.downgrade(cfg, "028")

    engine = sa.create_engine(cfg.get_main_option("sqlalchemy.url"))
    with engine.begin() as conn:
        with pytest.raises(sa.exc.ProgrammingError):
            conn.execute(sa.text("SELECT 1 FROM plans")).fetchone()
        cols = conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='organizations'"
            )
        ).fetchall()
        assert "plan_id" not in {c[0] for c in cols}
```

- [ ] **Step 2: Run the test**

```
.venv/bin/python3 -m pytest tests/test_migration_029.py -v
```
Expected: PASS in CI where `DATABASE_URL` is set; SKIPPED locally without it. Run locally via `docker compose exec auto-agent .venv/bin/python3 -m pytest tests/test_migration_029.py -v` to actually exercise it.

- [ ] **Step 3: Commit**

```bash
git add tests/test_migration_029.py
git commit -m "test(migrations): round-trip alembic 028 ↔ 029"
```

---

## Track B — Pricing module

### Task B1: `shared/pricing.py` with price-per-million-tokens table

**Files:**
- Create: `shared/pricing.py`
- Test: `tests/test_pricing.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pricing.py
from decimal import Decimal

import pytest

from shared.pricing import (
    DEFAULT_PRICE_CENTS_PER_MILLION,
    PRICE_PER_MILLION_TOKENS,
    estimate_cost_cents,
)


def test_known_model_costs_match_table() -> None:
    # 1M input + 500k output for sonnet-4-6 should equal:
    # input_per_M + 0.5 * output_per_M
    input_cents, output_cents = PRICE_PER_MILLION_TOKENS["claude-sonnet-4-6"]
    expected = Decimal(input_cents) + Decimal(output_cents) / 2
    assert estimate_cost_cents("claude-sonnet-4-6", 1_000_000, 500_000) == expected


def test_unknown_model_uses_default() -> None:
    cost = estimate_cost_cents("nonexistent-future-model-9000", 1_000_000, 0)
    assert cost == Decimal(DEFAULT_PRICE_CENTS_PER_MILLION[0])


def test_zero_tokens_zero_cost() -> None:
    assert estimate_cost_cents("claude-sonnet-4-6", 0, 0) == Decimal(0)
```

- [ ] **Step 2: Run test to verify it fails**

```
.venv/bin/python3 -m pytest tests/test_pricing.py -v
```
Expected: ImportError on `shared.pricing`.

- [ ] **Step 3: Create `shared/pricing.py`**

```python
"""Static price table for LLM calls. Cents per million tokens.

Numbers are estimates only — they go into `usage_events.cost_cents` for
operational visibility, not into billing. Phase 5 owns billing accuracy.

Source of truth: anthropic.com/pricing as of 2026-05-11. Update this table
when pricing changes. Treat additions as additive only — never delete a
key for a deprecated model, because old `usage_events` rows still reference
it indirectly via `model` text.
"""

from __future__ import annotations

from decimal import Decimal

# (input_cents_per_million, output_cents_per_million)
PRICE_PER_MILLION_TOKENS: dict[str, tuple[int, int]] = {
    "claude-sonnet-4-6": (300, 1500),       # $3 / $15
    "claude-opus-4-6": (1500, 7500),        # $15 / $75
    "claude-haiku-4-5": (80, 400),          # $0.80 / $4
    "claude-sonnet-4-20250514": (300, 1500),
    "claude-opus-4-20250514": (1500, 7500),
}

# Fallback for unknown / passthrough providers. Picked to slightly over-estimate
# so quota gates fail safe rather than letting cost-unknown traffic through.
DEFAULT_PRICE_CENTS_PER_MILLION: tuple[int, int] = (500, 2000)


def estimate_cost_cents(
    model: str, input_tokens: int, output_tokens: int
) -> Decimal:
    """Return estimated cost in cents (Decimal — supports fractions)."""
    in_cents_per_m, out_cents_per_m = PRICE_PER_MILLION_TOKENS.get(
        model, DEFAULT_PRICE_CENTS_PER_MILLION
    )
    cost = (
        Decimal(input_tokens) * Decimal(in_cents_per_m) / Decimal(1_000_000)
        + Decimal(output_tokens) * Decimal(out_cents_per_m) / Decimal(1_000_000)
    )
    return cost
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python3 -m pytest tests/test_pricing.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add shared/pricing.py tests/test_pricing.py
git commit -m "feat(pricing): static price-per-million-tokens table + estimate helper"
```

---

## Track C — Quotas helpers + usage emission

### Task C1: `shared/quotas.py` — pure read helpers

**Files:**
- Create: `shared/quotas.py`
- Test: `tests/test_quotas.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_quotas.py
"""Pure read helpers over usage_events / tasks / organizations.plans."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import (
    Organization, Plan, Task, TaskStatus, UsageEvent,
)
from shared import quotas


pytestmark = pytest.mark.asyncio


async def _seed_org_with_plan(session: AsyncSession, name: str) -> Organization:
    plan = Plan(
        name=f"plan-{name}",
        max_concurrent_tasks=1,
        max_tasks_per_day=5,
        max_input_tokens_per_day=1_000_000,
        max_output_tokens_per_day=250_000,
        max_members=3,
        monthly_price_cents=0,
    )
    session.add(plan)
    await session.flush()
    org = Organization(name=f"Org {name}", slug=f"org-{name}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    return org


async def test_get_plan_for_org_returns_attached_plan(session: AsyncSession) -> None:
    org = await _seed_org_with_plan(session, "a")
    plan = await quotas.get_plan_for_org(session, org.id)
    assert plan.name == "plan-a"


async def test_count_active_tasks_for_org_excludes_other_orgs(
    session: AsyncSession,
) -> None:
    org_a = await _seed_org_with_plan(session, "a")
    org_b = await _seed_org_with_plan(session, "b")
    session.add(Task(title="t1", description="", source="manual", source_id="x1",
                     status=TaskStatus.CODING, organization_id=org_a.id))
    session.add(Task(title="t2", description="", source="manual", source_id="x2",
                     status=TaskStatus.CODING, organization_id=org_b.id))
    await session.flush()
    assert await quotas.count_active_tasks_for_org(session, org_a.id) == 1
    assert await quotas.count_active_tasks_for_org(session, org_b.id) == 1


async def test_count_tasks_created_today_excludes_yesterday(
    session: AsyncSession,
) -> None:
    org = await _seed_org_with_plan(session, "t")
    today = dt.datetime.now(dt.timezone.utc)
    yesterday = today - dt.timedelta(days=1)
    session.add(Task(title="today", description="", source="manual", source_id="t1",
                     status=TaskStatus.QUEUED, organization_id=org.id,
                     created_at=today))
    session.add(Task(title="yest", description="", source="manual", source_id="t2",
                     status=TaskStatus.QUEUED, organization_id=org.id,
                     created_at=yesterday))
    await session.flush()
    n = await quotas.count_tasks_created_today(session, org.id)
    assert n == 1


async def test_sum_tokens_today_excludes_yesterday(session: AsyncSession) -> None:
    org = await _seed_org_with_plan(session, "tok")
    today = dt.datetime.now(dt.timezone.utc)
    yesterday = today - dt.timedelta(days=1)
    session.add(UsageEvent(
        org_id=org.id, kind="llm_call", model="x",
        input_tokens=100, output_tokens=50, cost_cents=Decimal(0),
        occurred_at=today,
    ))
    session.add(UsageEvent(
        org_id=org.id, kind="llm_call", model="x",
        input_tokens=999, output_tokens=999, cost_cents=Decimal(0),
        occurred_at=yesterday,
    ))
    await session.flush()
    in_today, out_today = await quotas.sum_tokens_today(session, org.id)
    assert in_today == 100
    assert out_today == 50


async def test_would_exceed_token_cap(session: AsyncSession) -> None:
    org = await _seed_org_with_plan(session, "cap")
    # Plan caps: input 1_000_000, output 250_000
    today = dt.datetime.now(dt.timezone.utc)
    session.add(UsageEvent(
        org_id=org.id, kind="llm_call", model="x",
        input_tokens=900_000, output_tokens=0, cost_cents=Decimal(0),
        occurred_at=today,
    ))
    await session.flush()
    # next call estimated at 200_000 input — 900k + 200k > 1M → exceeds
    assert await quotas.would_exceed_token_cap(session, org.id, est_input=200_000, est_output=0)
    # next call estimated at 50_000 input — 900k + 50k < 1M → ok
    assert not await quotas.would_exceed_token_cap(session, org.id, est_input=50_000, est_output=0)
```

(Tests assume a `session` fixture that yields an `AsyncSession` against a clean DB. The auto-agent suite already provides one — see `tests/conftest.py`.)

- [ ] **Step 2: Run test to verify it fails**

```
.venv/bin/python3 -m pytest tests/test_quotas.py -v
```
Expected: ImportError on `shared.quotas`.

- [ ] **Step 3: Create `shared/quotas.py`**

```python
"""Per-org quota lookups. Pure functions over an AsyncSession.

All time windows are UTC days (00:00:00–23:59:59.999999) — switch to per-org
TZ later if customers ask for it. v1 keeps it predictable.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from shared.models import Organization, Plan, Task, TaskStatus, UsageEvent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class QuotaExceeded(Exception):
    """Per-org quota violation. Surfaced as HTTP 429 by the router, or used
    to trigger BLOCKED_ON_QUOTA transitions inside the agent loop."""


# Mirrors orchestrator.queue.ACTIVE_STATUSES but kept local to avoid
# a cross-layer import. Update both together.
_ACTIVE_STATUSES = {
    TaskStatus.PLANNING,
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.AWAITING_CLARIFICATION,
    TaskStatus.CODING,
    TaskStatus.PR_CREATED,
    TaskStatus.AWAITING_CI,
    TaskStatus.AWAITING_REVIEW,
    TaskStatus.BLOCKED,
}


def _utc_day_bounds(now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    now = now or dt.datetime.now(dt.timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    return start, end


async def get_plan_for_org(session: AsyncSession, org_id: int) -> Plan:
    """Return the Plan attached to this org. Raises if org or plan missing."""
    q = await session.execute(
        select(Plan).join(Organization, Organization.plan_id == Plan.id).where(
            Organization.id == org_id
        )
    )
    plan = q.scalar_one_or_none()
    if plan is None:
        raise LookupError(f"No plan attached to org {org_id}")
    return plan


async def count_active_tasks_for_org(session: AsyncSession, org_id: int) -> int:
    q = await session.execute(
        select(func.count(Task.id)).where(
            Task.organization_id == org_id,
            Task.status.in_(_ACTIVE_STATUSES),
        )
    )
    return q.scalar_one()


async def count_tasks_created_today(session: AsyncSession, org_id: int) -> int:
    start, end = _utc_day_bounds()
    q = await session.execute(
        select(func.count(Task.id)).where(
            Task.organization_id == org_id,
            Task.created_at >= start,
            Task.created_at < end,
        )
    )
    return q.scalar_one()


async def sum_tokens_today(
    session: AsyncSession, org_id: int
) -> tuple[int, int]:
    start, end = _utc_day_bounds()
    q = await session.execute(
        select(
            func.coalesce(func.sum(UsageEvent.input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.output_tokens), 0),
        ).where(
            UsageEvent.org_id == org_id,
            UsageEvent.occurred_at >= start,
            UsageEvent.occurred_at < end,
        )
    )
    in_tokens, out_tokens = q.one()
    return int(in_tokens), int(out_tokens)


async def would_exceed_token_cap(
    session: AsyncSession, org_id: int, *, est_input: int, est_output: int
) -> bool:
    plan = await get_plan_for_org(session, org_id)
    in_used, out_used = await sum_tokens_today(session, org_id)
    return (
        in_used + est_input > plan.max_input_tokens_per_day
        or out_used + est_output > plan.max_output_tokens_per_day
    )
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python3 -m pytest tests/test_quotas.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add shared/quotas.py tests/test_quotas.py
git commit -m "feat(quotas): per-org read helpers (plans, tasks today, tokens today)"
```

### Task C2: `orchestrator/usage.py` — emit_usage_event

**Files:**
- Create: `orchestrator/usage.py`
- Test: covered by C3 (agent-loop integration)

- [ ] **Step 1: Create the module**

```python
"""Append a `usage_events` row for each accountable event.

For v1 we only emit `kind="llm_call"`. The function is async and idempotent
in failure-mode: if a write fails (e.g. DB transient) we log and swallow —
we do NOT want a quota-accounting failure to crash a paying customer's task.
"""

from __future__ import annotations

import structlog

from shared.database import async_session
from shared.models import UsageEvent
from shared.pricing import estimate_cost_cents

log = structlog.get_logger(__name__)


async def emit_usage_event(
    *,
    org_id: int,
    task_id: int | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    kind: str = "llm_call",
) -> None:
    """Insert one usage_events row. Best-effort; logs and continues on error."""
    cost = estimate_cost_cents(model, input_tokens, output_tokens)
    try:
        async with async_session() as session:
            session.add(
                UsageEvent(
                    org_id=org_id,
                    task_id=task_id,
                    kind=kind,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_cents=cost,
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001
        log.warning(
            "usage_event_write_failed",
            org_id=org_id,
            task_id=task_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
```

- [ ] **Step 2: Commit**

```bash
git add orchestrator/usage.py
git commit -m "feat(usage): emit_usage_event — append-only LLM accounting writer"
```

### Task C3: Wire usage emission + quota gate into `agent/loop.py`

**Files:**
- Modify: `agent/loop.py`
- Test: `tests/test_usage_event_emission.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_usage_event_emission.py
"""Agent loop emits one usage_events row per LLM call."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from agent.loop import AgentLoop, UsageSink
from agent.llm.types import LLMResponse, Message, TokenUsage
from shared.models import UsageEvent
from tests.helpers import make_org_and_task  # existing test helper


pytestmark = pytest.mark.asyncio


class _FakeProvider:
    is_passthrough = False
    model = "claude-sonnet-4-6"
    max_context_tokens = 200_000

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **_kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            message=Message(role="assistant", content="ok"),
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=100, output_tokens=50),
        )


async def test_usage_row_written_per_call(session) -> None:
    org, task = await make_org_and_task(session)
    sink = UsageSink(org_id=org.id, task_id=task.id)
    loop = AgentLoop(
        provider=_FakeProvider(),
        tools=None,
        workspace="/tmp",
        usage_sink=sink,
    )
    await loop.run("hi", system="sys")

    rows = (await session.execute(
        select(UsageEvent).where(UsageEvent.org_id == org.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].input_tokens == 100
    assert rows[0].output_tokens == 50
    assert rows[0].model == "claude-sonnet-4-6"
    assert rows[0].task_id == task.id
```

(If `tests/helpers.py` doesn't have `make_org_and_task`, create one as part of this task — small helper that inserts an Organization with a free Plan attached and a single Task row.)

- [ ] **Step 2: Run test to verify it fails**

```
.venv/bin/python3 -m pytest tests/test_usage_event_emission.py -v
```
Expected: ImportError on `agent.loop.UsageSink`.

- [ ] **Step 3: Add `UsageSink` and wire it into the agent loop**

In `agent/loop.py`, add near the top imports:

```python
from dataclasses import dataclass

from orchestrator.usage import emit_usage_event
from shared import quotas
from shared.database import async_session
```

After the existing dataclasses, add:

```python
from shared.quotas import QuotaExceeded  # canonical home — defined in Task C1 alongside the read helpers


@dataclass
class UsageSink:
    """Per-task accounting helper. Injected into AgentLoop.

    `emit` writes one row to usage_events (best-effort).
    `would_exceed_token_cap` returns True when the next call would cross
    the org's daily input/output token caps.
    """

    org_id: int
    task_id: int | None

    async def emit(self, *, model: str, usage) -> None:
        await emit_usage_event(
            org_id=self.org_id,
            task_id=self.task_id,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )

    async def would_exceed_token_cap(
        self, *, est_input: int, est_output: int
    ) -> bool:
        async with async_session() as session:
            return await quotas.would_exceed_token_cap(
                session, self.org_id,
                est_input=est_input, est_output=est_output,
            )
```

Modify `AgentLoop.__init__` to accept `usage_sink: UsageSink | None = None` and store it. In `_run_agentic`, find the line where `response = await self._provider.complete(...)` is awaited (around line 247 today) and wrap it like this:

```python
            # Quota pre-call gate (rough estimate based on max_tokens).
            if self._usage_sink is not None:
                approx_in = sum(
                    len(m.content or "") for m in api_messages
                ) // 4  # 4 chars/token rough
                if await self._usage_sink.would_exceed_token_cap(
                    est_input=approx_in, est_output=max_tokens,
                ):
                    raise QuotaExceeded(
                        f"Org {self._usage_sink.org_id} would exceed daily token cap"
                    )

            response = await self._provider.complete(
                messages=api_messages,
                tools=tool_defs if tool_defs else None,
                ...
            )

            # Post-call accounting (best-effort).
            if self._usage_sink is not None:
                await self._usage_sink.emit(
                    model=getattr(self._provider, "model", "unknown"),
                    usage=response.usage,
                )
```

Apply the same pre-call gate + post-call emit to the passthrough path in `_run_passthrough` (the CLI provider — emit with the single response's usage at the end; skip the pre-call gate since we don't have a tight estimate).

- [ ] **Step 4: Run tests to verify they pass**

```
.venv/bin/python3 -m pytest tests/test_usage_event_emission.py -v
```
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add agent/loop.py tests/test_usage_event_emission.py tests/helpers.py
git commit -m "feat(agent): emit usage_events + pre-call quota gate"
```

### Task C4: Construct `UsageSink` from the task-runner sites

**Files:**
- Modify: `agent/main.py` (and any other AgentLoop construction site)
- Test: covered by `tests/test_blocked_on_quota.py` in Track G

- [ ] **Step 1: Find every AgentLoop construction site**

```
grep -rn "AgentLoop(" --include="*.py"
```

For each call site that has a `Task` (or `task_id` + `organization_id`) in scope, construct a `UsageSink` and pass it:

```python
from agent.loop import AgentLoop, UsageSink

usage_sink = (
    UsageSink(org_id=task.organization_id, task_id=task.id)
    if task.organization_id is not None
    else None
)
loop = AgentLoop(
    provider=provider,
    tools=tools,
    workspace=workspace,
    usage_sink=usage_sink,
    # ... existing kwargs
)
```

- [ ] **Step 2: Commit**

```bash
git add agent/main.py  # plus any other modified files
git commit -m "feat(agent): wire UsageSink at task-runner construction sites"
```

---

## Track D — Workspace dirs per org

### Task D1: `clone_repo` / `cleanup_workspace` accept `organization_id`

**Files:**
- Modify: `agent/workspace.py`
- Test: `tests/test_workspace_per_org_dirs.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_workspace_per_org_dirs.py
from __future__ import annotations

import os

from agent import workspace as ws


def test_per_org_workspace_path_when_org_id_set(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ws, "WORKSPACES_DIR", str(tmp_path))
    expected = os.path.join(str(tmp_path), "42", "task-7")
    actual = ws._workspace_path(task_id=7, organization_id=42)
    assert actual == expected


def test_legacy_workspace_path_when_org_id_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ws, "WORKSPACES_DIR", str(tmp_path))
    expected = os.path.join(str(tmp_path), "task-7")
    actual = ws._workspace_path(task_id=7, organization_id=None)
    assert actual == expected


def test_cleanup_workspace_only_removes_per_org_subtree(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(ws, "WORKSPACES_DIR", str(tmp_path))
    a = os.path.join(str(tmp_path), "1", "task-7")
    b = os.path.join(str(tmp_path), "2", "task-7")
    os.makedirs(a)
    os.makedirs(b)

    ws.cleanup_workspace(task_id=7, organization_id=1)

    assert not os.path.exists(a)
    assert os.path.exists(b)  # other org untouched
```

- [ ] **Step 2: Run test to verify it fails**

```
.venv/bin/python3 -m pytest tests/test_workspace_per_org_dirs.py -v
```
Expected: AttributeError — `_workspace_path` not defined; `cleanup_workspace` signature mismatch.

- [ ] **Step 3: Refactor `agent/workspace.py`**

Add the helper near the top:

```python
def _workspace_path(*, task_id: int, organization_id: int | None) -> str:
    if organization_id is not None:
        return os.path.join(WORKSPACES_DIR, str(organization_id), f"task-{task_id}")
    return os.path.join(WORKSPACES_DIR, f"task-{task_id}")
```

Modify `clone_repo`:

```python
async def clone_repo(
    repo_url: str,
    task_id: int,
    default_branch: str = "main",
    workspace_name: str | None = None,
    fallback_branch: str | None = None,
    *,
    user_id: int | None = None,
    organization_id: int | None = None,
) -> str:
    # ...
    if workspace_name:
        workspace = os.path.join(WORKSPACES_DIR, workspace_name)
    else:
        workspace = _workspace_path(task_id=task_id, organization_id=organization_id)
    # rest unchanged
```

Modify `cleanup_workspace`:

```python
def cleanup_workspace(task_id: int, organization_id: int | None = None) -> None:
    workspace = _workspace_path(task_id=task_id, organization_id=organization_id)
    if os.path.exists(workspace):
        shutil.rmtree(workspace)
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python3 -m pytest tests/test_workspace_per_org_dirs.py -v tests/test_workspace_clone_stale_dir.py -v
```
Expected: new tests pass, existing tests pass (legacy path preserved when `organization_id=None`).

- [ ] **Step 5: Commit**

```bash
git add agent/workspace.py tests/test_workspace_per_org_dirs.py
git commit -m "feat(workspace): per-org subdirectories when organization_id supplied"
```

### Task D2: Thread `organization_id` from lifecycle handlers

**Files:**
- Modify: `agent/lifecycle/coding.py`, `agent/lifecycle/planning.py`, `agent/lifecycle/cleanup.py`
- Test: existing lifecycle tests (no new test — refactor only)

- [ ] **Step 1: Find every call to `clone_repo` / `cleanup_workspace`**

```
grep -rn "clone_repo\|cleanup_workspace" --include="*.py" agent/lifecycle
```

For each call site, the surrounding code already loads a `Task` — pass `organization_id=task.organization_id`:

```python
# coding.py:38 area
from agent.workspace import cleanup_workspace, clone_repo

# at the clone site:
workspace = await clone_repo(
    repo.url, task.id, default_branch=repo.default_branch,
    user_id=task.created_by_user_id,
    organization_id=task.organization_id,
)

# at the cleanup site:
cleanup_workspace(task_id, organization_id=task.organization_id)
```

(For `cleanup.py:19` — `cleanup_workspace(task_id)` — load the task first to read `organization_id`, or accept it as a parameter from the event payload. The minimal change: have the event-publisher include `organization_id` so the handler can pass it down without an extra DB read.)

- [ ] **Step 2: Run the full suite to catch regressions**

```
.venv/bin/python3 -m pytest tests/ -q
```
Expected: 808 + the Phase 4 tests added so far, all passing.

- [ ] **Step 3: Commit**

```bash
git add agent/lifecycle/coding.py agent/lifecycle/planning.py agent/lifecycle/cleanup.py
git commit -m "refactor(lifecycle): thread organization_id into workspace ops"
```

---

## Track E — Queue per-org cap

### Task E1: Add `_org_at_concurrency_cap` + use it in dispatcher

**Files:**
- Modify: `orchestrator/queue.py`
- Test: `tests/test_queue_per_org_cap.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_queue_per_org_cap.py
"""Per-org cap blocks org A but lets org B through."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator import queue as q
from shared.models import Organization, Plan, Task, TaskStatus


pytestmark = pytest.mark.asyncio


async def _seed_plan(session: AsyncSession, cap: int) -> Plan:
    plan = Plan(
        name=f"plan-cap-{cap}",
        max_concurrent_tasks=cap,
        max_tasks_per_day=100,
        max_input_tokens_per_day=10_000_000,
        max_output_tokens_per_day=2_500_000,
        max_members=5,
    )
    session.add(plan)
    await session.flush()
    return plan


async def _seed_org(session, plan: Plan, slug: str) -> Organization:
    org = Organization(name=slug, slug=slug, plan_id=plan.id)
    session.add(org)
    await session.flush()
    return org


async def test_org_at_cap_skipped_other_org_dispatched(session: AsyncSession) -> None:
    plan = await _seed_plan(session, cap=1)
    org_a = await _seed_org(session, plan, "a")
    org_b = await _seed_org(session, plan, "b")

    # org A: one task already CODING (at cap), one QUEUED
    session.add(Task(title="a-active", description="", source="manual", source_id="s1",
                     status=TaskStatus.CODING, organization_id=org_a.id))
    session.add(Task(title="a-queued", description="", source="manual", source_id="s2",
                     status=TaskStatus.QUEUED, organization_id=org_a.id, priority=1))
    # org B: one QUEUED, lower priority (later in the iteration order)
    session.add(Task(title="b-queued", description="", source="manual", source_id="s3",
                     status=TaskStatus.QUEUED, organization_id=org_b.id, priority=2))
    await session.flush()

    picked = await q.next_eligible_task(session)
    assert picked is not None
    assert picked.title == "b-queued"  # org A skipped, org B picked
```

- [ ] **Step 2: Run test to verify it fails**

```
.venv/bin/python3 -m pytest tests/test_queue_per_org_cap.py -v
```
Expected: org A's task picked because there's no per-org cap yet.

- [ ] **Step 3: Extend `orchestrator/queue.py`**

Add a helper and consult it in `next_eligible_task` + `can_start_task`:

```python
from shared import quotas


async def _org_at_concurrency_cap(session: AsyncSession, org_id: int) -> bool:
    """True when the org has hit its plan's max_concurrent_tasks."""
    plan = await quotas.get_plan_for_org(session, org_id)
    active = await quotas.count_active_tasks_for_org(session, org_id)
    return active >= plan.max_concurrent_tasks


async def can_start_task(session: AsyncSession, task: Task) -> bool:
    if await count_active(session) >= settings.max_concurrent_workers:
        return False
    if task.organization_id is not None and await _org_at_concurrency_cap(
        session, task.organization_id
    ):
        return False
    return not (
        task.repo_id is not None
        and await _repo_has_active_task(session, task.repo_id)
    )


async def next_eligible_task(session: AsyncSession) -> Task | None:
    if await count_active(session) >= settings.max_concurrent_workers:
        return None

    active_repos_q = await session.execute(
        select(Task.repo_id)
        .where(Task.status.in_(ACTIVE_STATUSES), Task.repo_id.is_not(None))
        .distinct()
    )
    busy_repos = {row[0] for row in active_repos_q.all()}

    capped_orgs: set[int] = set()  # memoize per-tick

    queued_q = await session.execute(
        select(Task)
        .where(Task.status == TaskStatus.QUEUED)
        .order_by(Task.priority.asc(), Task.created_at.asc())
    )
    for t in queued_q.scalars():
        if t.repo_id is not None and t.repo_id in busy_repos:
            continue
        if t.organization_id is not None:
            if t.organization_id in capped_orgs:
                continue
            if await _org_at_concurrency_cap(session, t.organization_id):
                capped_orgs.add(t.organization_id)
                continue
        return t
    return None
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python3 -m pytest tests/test_queue_per_org_cap.py tests/test_queue_multi_tenant.py -v
```
Expected: both files green.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/queue.py tests/test_queue_per_org_cap.py
git commit -m "feat(queue): per-org concurrency cap from plan.max_concurrent_tasks"
```

---

## Track F — Rate limit task creation

### Task F1: Enforce `max_tasks_per_day` on `POST /api/tasks`

**Files:**
- Modify: `orchestrator/router.py`
- Test: `tests/test_rate_limit_task_create.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rate_limit_task_create.py
"""POST /api/tasks 429s when org is over plan.max_tasks_per_day."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from shared.models import Organization, Plan, Task, TaskStatus


pytestmark = pytest.mark.asyncio


@pytest.fixture
def app_with_overridden_org(session):
    """Build a FastAPI app mounting just the orchestrator router, with
    current_org_id_dep overridden to return a fixed org_id."""
    from orchestrator.auth import current_org_id_dep
    from orchestrator.router import router

    async def _seed_org() -> int:
        plan = Plan(
            name="tight", max_concurrent_tasks=10,
            max_tasks_per_day=2,
            max_input_tokens_per_day=10_000_000,
            max_output_tokens_per_day=10_000_000,
            max_members=10,
        )
        session.add(plan)
        await session.flush()
        org = Organization(name="t", slug="t-rate", plan_id=plan.id)
        session.add(org)
        await session.flush()
        return org.id

    org_id_holder: dict[str, int] = {}

    a = FastAPI()
    a.include_router(router, prefix="/api")

    async def _fake_org():
        if "id" not in org_id_holder:
            org_id_holder["id"] = await _seed_org()
        return org_id_holder["id"]

    a.dependency_overrides[current_org_id_dep] = _fake_org
    return a


async def test_create_returns_429_when_over_daily_cap(app_with_overridden_org) -> None:
    body = {"title": "x", "description": "y", "source": "manual"}
    async with AsyncClient(
        transport=ASGITransport(app=app_with_overridden_org),
        base_url="http://t",
    ) as c:
        for i in range(2):
            r = await c.post("/api/tasks", json={**body, "source_id": f"i{i}"})
            assert r.status_code == 200, r.text

        r = await c.post("/api/tasks", json={**body, "source_id": "i2"})
        assert r.status_code == 429
        assert "Retry-After" in r.headers
        assert "daily task limit" in r.json()["detail"].lower()
```

Note: `create_task` currently derives `caller_org_id` from the JWT cookie/header, not from `current_org_id_dep`. Refactor `create_task` to consume `current_org_id_dep` so the test override applies. (Alternative: keep current cookie parsing but also accept the dep when no cookie is supplied. Pick whichever keeps the diff smaller — the dep approach is cleaner.)

- [ ] **Step 2: Run test to verify it fails**

```
.venv/bin/python3 -m pytest tests/test_rate_limit_task_create.py -v
```
Expected: third POST returns 200 (no rate limit) → test fails on `assert r.status_code == 429`.

- [ ] **Step 3: Add `quotas.enforce_task_create_limit` + call from `create_task`**

In `shared/quotas.py`, append (`QuotaExceeded` is already defined at the top of the file from Task C1):

```python
async def enforce_task_create_limit(session: AsyncSession, org_id: int) -> None:
    """Raise `QuotaExceeded` if creating one more task would breach today's cap."""
    plan = await get_plan_for_org(session, org_id)
    n = await count_tasks_created_today(session, org_id)
    if n >= plan.max_tasks_per_day:
        raise QuotaExceeded(
            f"Daily task limit reached ({plan.max_tasks_per_day}). "
            f"Resets at UTC midnight."
        )
```

In `orchestrator/router.py`, in `create_task` (line 758) immediately after `caller_org_id` is resolved (around line 779):

```python
if caller_org_id is not None:
    try:
        await quotas.enforce_task_create_limit(session, caller_org_id)
    except quotas.QuotaExceeded as e:
        raise HTTPException(
            status_code=429,
            detail=str(e),
            headers={"Retry-After": str(_seconds_until_utc_midnight())},
        )
```

Add `_seconds_until_utc_midnight` near the other router helpers:

```python
def _seconds_until_utc_midnight() -> int:
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    tomorrow = (now + _dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int((tomorrow - now).total_seconds())
```

Import `quotas` at the top:

```python
from shared import quotas
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python3 -m pytest tests/test_rate_limit_task_create.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/quotas.py orchestrator/router.py tests/test_rate_limit_task_create.py
git commit -m "feat(rate-limit): 429 task creation over plan.max_tasks_per_day"
```

---

## Track G — BLOCKED_ON_QUOTA on token-cap exhaustion

### Task G1: Catch `QuotaExceeded` in task runners, transition to `BLOCKED_ON_QUOTA`

**Files:**
- Modify: `agent/main.py` (event handlers — planning, coding, review)
- Test: `tests/test_blocked_on_quota.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_blocked_on_quota.py
"""Token-cap exhaustion transitions in-flight task to BLOCKED_ON_QUOTA."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import select

from shared.models import Task, TaskStatus, UsageEvent
from agent.loop import QuotaExceeded
from tests.helpers import make_org_and_task


pytestmark = pytest.mark.asyncio


async def test_quota_exceeded_during_coding_transitions_task(session) -> None:
    org, task = await make_org_and_task(session, status=TaskStatus.CODING)

    # Patch AgentLoop.run to raise QuotaExceeded on the first call.
    async def _raise(*a, **kw):
        raise QuotaExceeded(f"Org {org.id} would exceed daily token cap")

    with patch("agent.main.AgentLoop.run", side_effect=_raise):
        from agent.main import on_task_start_coding
        from shared.events import Event
        await on_task_start_coding(Event(type="task.start_coding", task_id=task.id))

    await session.refresh(task)
    assert task.status == TaskStatus.BLOCKED_ON_QUOTA
```

- [ ] **Step 2: Run test to verify it fails**

```
.venv/bin/python3 -m pytest tests/test_blocked_on_quota.py -v
```
Expected: task probably ends up in `FAILED` or remains in `CODING`.

- [ ] **Step 3: Add a `try/except QuotaExceeded` wrapper around the AgentLoop runs in `agent/main.py`**

In each handler (`on_task_start_planning`, `on_task_start_coding`, `on_task_start_review`), wrap the existing `await loop.run(...)`:

```python
from agent.loop import AgentLoop, QuotaExceeded, UsageSink
from orchestrator.state_machine import transition

try:
    result = await loop.run(prompt, system=system)
except QuotaExceeded as e:
    log.info("task_blocked_on_quota", task_id=task.id, reason=str(e))
    async with async_session() as s:
        t = await get_task(s, task.id)
        if t is not None:
            await transition(s, t, TaskStatus.BLOCKED_ON_QUOTA, str(e))
            await s.commit()
    return
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python3 -m pytest tests/test_blocked_on_quota.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/main.py tests/test_blocked_on_quota.py
git commit -m "feat(agent): transition to BLOCKED_ON_QUOTA on token-cap hit"
```

### Task G2: Auto-unblock at UTC midnight (Postgres-driven)

**Files:**
- Modify: `run.py` (or wherever the periodic poller lives)
- Test: `tests/test_blocked_on_quota_unblock.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_blocked_on_quota_unblock.py
"""When today's token usage is under the cap, BLOCKED_ON_QUOTA tasks → QUEUED."""
from __future__ import annotations

import pytest

from orchestrator.unblock import unblock_quota_paused
from shared.models import TaskStatus
from tests.helpers import make_org_and_task


pytestmark = pytest.mark.asyncio


async def test_under_cap_unblocks(session) -> None:
    org, task = await make_org_and_task(session, status=TaskStatus.BLOCKED_ON_QUOTA)
    # No usage_events for today — under cap.
    await unblock_quota_paused(session)
    await session.refresh(task)
    assert task.status == TaskStatus.QUEUED
```

- [ ] **Step 2: Create `orchestrator/unblock.py`**

```python
"""Sweep that promotes BLOCKED_ON_QUOTA → QUEUED when today's usage is back under the cap.

Runs on the same periodic interval as the queue dispatcher in run.py.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select

from orchestrator.state_machine import transition
from shared import quotas
from shared.models import Task, TaskStatus

log = structlog.get_logger(__name__)


async def unblock_quota_paused(session) -> int:
    """Return the number of tasks moved back to QUEUED."""
    q = await session.execute(
        select(Task).where(Task.status == TaskStatus.BLOCKED_ON_QUOTA)
    )
    moved = 0
    for t in q.scalars():
        if t.organization_id is None:
            continue
        in_used, out_used = await quotas.sum_tokens_today(session, t.organization_id)
        plan = await quotas.get_plan_for_org(session, t.organization_id)
        if (
            in_used < plan.max_input_tokens_per_day
            and out_used < plan.max_output_tokens_per_day
        ):
            await transition(session, t, TaskStatus.QUEUED, "Quota window reset")
            moved += 1
    if moved:
        await session.commit()
        log.info("unblocked_quota_paused_tasks", count=moved)
    return moved
```

- [ ] **Step 3: Wire into the main poll loop in `run.py`**

Find the periodic tick that calls `_try_start_queued` (search for the `async def`). Add a call to `unblock_quota_paused` immediately before it:

```python
from orchestrator.unblock import unblock_quota_paused

async with async_session() as session:
    await unblock_quota_paused(session)
    await _try_start_queued(session)
```

- [ ] **Step 4: Run tests**

```
.venv/bin/python3 -m pytest tests/test_blocked_on_quota_unblock.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/unblock.py run.py tests/test_blocked_on_quota_unblock.py
git commit -m "feat(quota): sweep BLOCKED_ON_QUOTA → QUEUED when under cap"
```

---

## Track H — Settings UI: usage page

### Task H1: `GET /api/usage/summary` backend endpoint

**Files:**
- Modify: `orchestrator/router.py`, `shared/types.py`
- Test: `tests/test_usage_endpoint.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_usage_endpoint.py
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.helpers import make_org_and_task


pytestmark = pytest.mark.asyncio


async def test_usage_summary_returns_plan_and_current_totals(session) -> None:
    org, _task = await make_org_and_task(session, slug="usage-test")

    from orchestrator.auth import current_org_id_dep
    from orchestrator.router import router

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _fake_org() -> int:
        return org.id

    app.dependency_overrides[current_org_id_dep] = _fake_org

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/usage/summary")

    assert r.status_code == 200
    payload = r.json()
    assert payload["plan"]["name"] == "free"
    assert payload["active_tasks"] >= 0
    assert payload["tasks_today"] >= 1  # we just created one
    assert payload["input_tokens_today"] == 0
    assert payload["output_tokens_today"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

```
.venv/bin/python3 -m pytest tests/test_usage_endpoint.py -v
```
Expected: 404.

- [ ] **Step 3: Add Pydantic types**

In `shared/types.py`:

```python
class PlanRead(BaseModel):
    id: int
    name: str
    max_concurrent_tasks: int
    max_tasks_per_day: int
    max_input_tokens_per_day: int
    max_output_tokens_per_day: int


class UsageSummary(BaseModel):
    plan: PlanRead
    active_tasks: int
    tasks_today: int
    input_tokens_today: int
    output_tokens_today: int
```

- [ ] **Step 4: Add the endpoint to `orchestrator/router.py`**

```python
@router.get("/usage/summary", response_model=UsageSummary)
async def get_usage_summary(
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> UsageSummary:
    plan = await quotas.get_plan_for_org(session, org_id)
    active = await quotas.count_active_tasks_for_org(session, org_id)
    today_n = await quotas.count_tasks_created_today(session, org_id)
    in_tok, out_tok = await quotas.sum_tokens_today(session, org_id)
    return UsageSummary(
        plan=PlanRead.model_validate(plan, from_attributes=True),
        active_tasks=active,
        tasks_today=today_n,
        input_tokens_today=in_tok,
        output_tokens_today=out_tok,
    )
```

- [ ] **Step 5: Run tests**

```
.venv/bin/python3 -m pytest tests/test_usage_endpoint.py -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add shared/types.py orchestrator/router.py tests/test_usage_endpoint.py
git commit -m "feat(api): GET /api/usage/summary"
```

### Task H2: web-next typed client + hook

**Files:**
- Create: `web-next/lib/usage.ts`, `web-next/hooks/useUsage.ts`

- [ ] **Step 1: Create `web-next/lib/usage.ts`**

```ts
import { apiFetch } from "./api";

export interface PlanRead {
  id: number;
  name: string;
  max_concurrent_tasks: number;
  max_tasks_per_day: number;
  max_input_tokens_per_day: number;
  max_output_tokens_per_day: number;
}

export interface UsageSummary {
  plan: PlanRead;
  active_tasks: number;
  tasks_today: number;
  input_tokens_today: number;
  output_tokens_today: number;
}

export async function fetchUsageSummary(): Promise<UsageSummary> {
  return apiFetch<UsageSummary>("/usage/summary");
}
```

- [ ] **Step 2: Create `web-next/hooks/useUsage.ts`**

```ts
import { useQuery } from "@tanstack/react-query";
import { fetchUsageSummary, UsageSummary } from "../lib/usage";

export function useUsageSummary() {
  return useQuery<UsageSummary>({
    queryKey: ["usage", "summary"],
    queryFn: fetchUsageSummary,
    refetchInterval: 60_000,
  });
}
```

- [ ] **Step 3: Run `tsc` to check types**

```
cd web-next && npx tsc --noEmit
```
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add web-next/lib/usage.ts web-next/hooks/useUsage.ts
git commit -m "feat(web-next): typed usage summary client + hook"
```

### Task H3: `/settings/usage` page

**Files:**
- Create: `web-next/app/(app)/settings/usage/page.tsx`
- Modify: `web-next/app/(app)/settings/layout.tsx` (or sidebar — mirror Phase 3)

- [ ] **Step 1: Create the page**

```tsx
// web-next/app/(app)/settings/usage/page.tsx
"use client";

import { useUsageSummary } from "@/hooks/useUsage";

function Bar({ label, used, cap }: { label: string; used: number; cap: number }) {
  const pct = cap > 0 ? Math.min(100, Math.round((used / cap) * 100)) : 0;
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-sm">
        <span>{label}</span>
        <span className="tabular-nums text-muted-foreground">
          {used.toLocaleString()} / {cap.toLocaleString()}
        </span>
      </div>
      <div className="h-2 w-full rounded bg-muted">
        <div
          className="h-2 rounded bg-primary"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export default function UsagePage() {
  const { data, isLoading, error } = useUsageSummary();

  if (isLoading) return <div className="p-6">Loading usage…</div>;
  if (error || !data)
    return <div className="p-6 text-destructive">Failed to load usage.</div>;

  const { plan } = data;
  return (
    <div className="p-6 max-w-3xl space-y-8">
      <header>
        <h1 className="text-2xl font-semibold">Usage</h1>
        <p className="text-muted-foreground mt-1">
          Your current plan and today's consumption (UTC day).
        </p>
      </header>

      <section className="rounded-lg border p-6 space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-sm text-muted-foreground">Plan</div>
            <div className="text-xl font-medium capitalize">{plan.name}</div>
          </div>
          <button
            disabled
            className="rounded border px-3 py-1.5 text-sm opacity-60"
            title="Billing arrives in Phase 5"
          >
            Upgrade
          </button>
        </div>
        <Bar label="Active tasks" used={data.active_tasks} cap={plan.max_concurrent_tasks} />
        <Bar label="Tasks today" used={data.tasks_today} cap={plan.max_tasks_per_day} />
        <Bar
          label="Input tokens today"
          used={data.input_tokens_today}
          cap={plan.max_input_tokens_per_day}
        />
        <Bar
          label="Output tokens today"
          used={data.output_tokens_today}
          cap={plan.max_output_tokens_per_day}
        />
      </section>
    </div>
  );
}
```

- [ ] **Step 2: Add "Usage" to the settings nav**

Open `web-next/app/(app)/settings/layout.tsx` (or whichever file declares the settings sub-nav — match the Phase 3 pattern that added "Integrations"). Add an entry:

```tsx
{ href: "/settings/usage", label: "Usage" },
```

- [ ] **Step 3: Run `tsc`**

```
cd web-next && npx tsc --noEmit
```
Expected: clean.

- [ ] **Step 4: Run the dev server and click through**

```
cd web-next && npm run dev
```
Open `http://localhost:3000/settings/usage` in a browser. Confirm the four bars render with real data from the backend.

- [ ] **Step 5: Commit**

```bash
git add web-next/app/(app)/settings/usage/page.tsx web-next/app/(app)/settings/layout.tsx
git commit -m "feat(web-next): /settings/usage page with plan card and quota bars"
```

---

## Track I — Full-suite verification + handover

### Task I1: Run the full Python test suite

- [ ] **Step 1: Run unit tests**

```
.venv/bin/python3 -m pytest tests/ -q
```
Expected: previous count + Phase 4 additions (≈ 16 new tests), 0 failures.

- [ ] **Step 2: Run ruff lint**

```
ruff check .
```
Expected: count at or below the pre-Phase-4 baseline. Fix anything new.

- [ ] **Step 3: Format check**

```
ruff format --check .
```
If any files need formatting, run `ruff format .` and commit the fix.

- [ ] **Step 4: Commit any lint/format fixes**

```bash
git add -p  # review changes
git commit -m "chore: ruff format/lint after Phase 4"
```

### Task I2: Write the Phase 4 handover

**Files:**
- Create: `docs/superpowers/plans/2026-05-11-handover-after-phase-4.md`

- [ ] **Step 1: Write the handover**

Mirror the structure of `docs/superpowers/plans/2026-05-11-handover-after-phase-3.md`. Cover:

1. **What's done** — one bullet block per Track (A–H) with file paths and line refs.
2. **Production state** — env vars needed (none new), migration command, smoke tests.
3. **What's NOT done** — billing/Stripe (Phase 5), Redis-backed counters (deferred), per-org socket tokens, weighted round-robin.
4. **Critical things to know** — UTC day boundary, BLOCKED_ON_QUOTA exit path, usage event best-effort semantics, plan_id default = free, the implicit reset behavior of `unblock_quota_paused`.
5. **Where to find things** — file map.
6. **Out of scope** — quota notifications (DM/email when at 80% / 100%), weighted dispatch, monthly-spend caps, plan downgrade UX.
7. **Conversation summary** — written at end of execution.

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/plans/2026-05-11-handover-after-phase-4.md
git commit -m "docs(handover): post-Phase-4 handover"
```

### Task I3: Open the PR

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feat/phase-4-quotas
```

- [ ] **Step 2: Open a draft PR**

```bash
gh pr create --draft --title "Phase 4: resource isolation + quotas" --body "$(cat <<'EOF'
## Summary
- Migration 029: plans + per-org plan_id + usage_events
- Per-org concurrency cap from plan.max_concurrent_tasks
- Daily task-create cap → 429 with Retry-After
- LLM token cap → BLOCKED_ON_QUOTA + auto-unblock sweep
- /settings/usage page with plan card and four quota bars

## Test plan
- [ ] All previous tests pass (808 + Phase 4 additions)
- [ ] `alembic upgrade head` then `alembic downgrade -1` on a real DB
- [ ] Smoke: create tasks past free-plan daily cap → 429
- [ ] Smoke: simulate token exhaustion → task in BLOCKED_ON_QUOTA
- [ ] Smoke: /settings/usage renders against live data

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Out of scope (intentionally deferred)

- **Billing / Stripe / paywall** — Phase 5.
- **Notifications when at 80% / 100% of cap** — important for UX but not gating. Phase 4.1 or rolled into Phase 6 alerting work.
- **Weighted round-robin (Pro orgs 3:1 over Free)** — spec says start simple. Add only after we have signal that Free orgs are starving Pro.
- **Monthly spend caps** — daily is the v1 unit.
- **Per-user (not per-org) caps** — orgs are the billing/limit boundary; users inside one org share quota.
- **Aggregated `usage_summaries` rollup table** — for v1 we read raw `usage_events`. Add a daily-rolled-up table when the per-org query gets slow (>100ms in p95).
- **Per-org Redis counters** — Postgres aggregates are sufficient at our task volume. Add Redis when create endpoints' p95 latency exceeds 100ms attributable to the count query.
- **Disk-quota enforcement** — workspace dirs are per-org but we don't cap on-disk size. Cleanup script is best-effort.

---

## Self-review notes (filled by author before handing off)

- **Spec coverage:** every bullet in `2026-05-09-multi-tenant-saas-implementation.md` §Phase 4 maps to a task above. Workspace dirs → D. Queue caps → E. Usage tracking → C. Rate limits → F + G. Plan model → A. Settings UI → H.
- **No placeholders:** every step has either complete code, an exact grep/run command, or a commit instruction.
- **Type consistency:** `UsageSink`, `QuotaExceeded`, `_workspace_path`, `unblock_quota_paused` are spelled the same in every reference. `QuotaExceeded` has one canonical home (`shared/quotas.py`, defined in Task C1) and is imported elsewhere. `BLOCKED_ON_QUOTA` enum value is `"blocked_on_quota"` in models, tests, and migration discussion. `Plan.max_concurrent_tasks` (snake_case) used everywhere.
- **Test infrastructure:** the existing suite is mock-heavy; Phase 4's quota math is multi-table and tedious to mock. Added Task A0 to introduce a real-DB `session` fixture (skip-if no `DATABASE_URL`) and `tests/helpers.py::make_org_and_task`. HTTP tests use the existing `ASGITransport` + `dependency_overrides` pattern from `tests/test_slack_oauth.py`.
- **Known gap to call out at execution time:** `create_task` in `orchestrator/router.py` currently derives `caller_org_id` from the JWT cookie/header, not from `current_org_id_dep`. Task F1 notes this and asks the executor to refactor it to use the dep so the test override works; that refactor is small but is the only structural change to an existing endpoint in this phase.
