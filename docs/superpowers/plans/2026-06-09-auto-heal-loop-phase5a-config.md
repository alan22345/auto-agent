# Auto-Heal Loop — Phase 5a: HealthLoopConfig model + migration + config service

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Checkbox steps.

**Goal:** Per-repo persistence for the auto-heal loop — the `HealthLoopConfig` ORM model, its alembic migration, and an async config service (get-or-create, enable/disable, state, suppress/unsuppress findings, batch_size).

**Architecture:** A new `Base` model in `shared/models/core.py` (mirroring `RepoGraphConfig`), exported from `shared/models/__init__.py`; alembic migration `055` chaining off `054`; an async service `agent/health_loop/config_service.py` over `shared.database.async_session` (agent→shared is allowed; mirrors `agent/po_analyzer.py` DB access).

**Verification boundary:** locally `DATABASE_URL` is unset → DB CRUD tests SKIP (repo pattern) and the Postgres `JSONB` column can't use SQLite. So: model structure, migration import + revision chain, and a pure suppression-dedup helper are tested locally; CRUD behavior is verified on the VM/CI. Do NOT fake DB behavior to get green.

**Spec:** `docs/superpowers/specs/2026-06-09-auto-heal-loop-design.md` (Data model). Map: model style from `shared/models/core.py::RepoGraphConfig`; migration template `migrations/versions/053_repo_graph_flow_json.py`; head revision `054`.

---

## File structure

- **Modify:** `shared/models/core.py` — add `HealthLoopConfig`.
- **Modify:** `shared/models/__init__.py` — export `HealthLoopConfig`.
- **Create:** `migrations/versions/055_health_loop_config.py` — create table.
- **Create:** `agent/health_loop/config_service.py` — async CRUD + a pure dedup helper.
- **Create:** `tests/test_health_loop_config.py` — structural + pure-helper tests (+ skip-guarded CRUD).

---

### Task 1: `HealthLoopConfig` model + export

**Files:** Modify `shared/models/core.py`, `shared/models/__init__.py`. Test: `tests/test_health_loop_config.py`.

- [ ] **Step 1: Write the failing test (pure — model structure)**

Create `tests/test_health_loop_config.py`:

```python
"""Phase 5a — HealthLoopConfig model + config service."""
from __future__ import annotations


def test_health_loop_config_columns():
    from shared.models import HealthLoopConfig

    cols = {c.name for c in HealthLoopConfig.__table__.columns}
    assert cols == {
        "repo_id",
        "organization_id",
        "enabled",
        "cleanup_branch",
        "batch_size",
        "state",
        "suppressed_finding_hashes",
        "supervisor_task_id",
        "last_run_at",
        "created_at",
        "updated_at",
    }
    assert HealthLoopConfig.__tablename__ == "health_loop_configs"
    # repo_id is the PK (1:1 with repo).
    pk = {c.name for c in HealthLoopConfig.__table__.primary_key.columns}
    assert pk == {"repo_id"}
```

- [ ] **Step 2: Run the test — verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_config.py -k columns -q`
Expected: FAIL — `ImportError: cannot import name 'HealthLoopConfig'`

- [ ] **Step 3: Implement the model**

In `shared/models/core.py`, add after the `RepoGraphConfig` class (use the same imports already present in the file — `Column`, `Integer`, `String`, `Boolean`, `DateTime`, `ForeignKey`, `JSONB`, `text`, `_utcnow`):

```python
class HealthLoopConfig(Base):
    """Per-repo auto-heal loop settings.

    One row per repo with the loop configured. ``repo_id`` PK keeps the 1:1
    relationship explicit. ``state`` is the supervisor's lifecycle
    (``idle`` waiting for findings / ``running`` working a batch /
    ``paused`` stopped by the user). ``suppressed_finding_hashes`` is the
    won't-fix list the ranker filters out.
    """

    __tablename__ = "health_loop_configs"

    repo_id = Column(
        Integer,
        ForeignKey("repos.id", ondelete="CASCADE"),
        primary_key=True,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    enabled = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    cleanup_branch = Column(
        String(255),
        nullable=False,
        default="auto-agent/health-cleanup",
        server_default="auto-agent/health-cleanup",
    )
    batch_size = Column(Integer, nullable=False, default=5, server_default="5")
    # idle | running | paused
    state = Column(String(16), nullable=False, default="idle", server_default="idle")
    suppressed_finding_hashes = Column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    supervisor_task_id = Column(
        Integer,
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )
```

> Confirm `text`, `JSONB`, and `_utcnow` are already imported at the top of `core.py` (they are used by `RepoGraph`/`RepoGraphConfig`). If any is missing, add it to the existing import group.

In `shared/models/__init__.py`, add `HealthLoopConfig` to the `from .core import (...)` list (keep it alphabetical with the others — it goes between `GitHubInstallation` and `MessengerConversation`).

- [ ] **Step 4: Run the test — verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_config.py -k columns -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/models/core.py shared/models/__init__.py tests/test_health_loop_config.py
git commit -m "feat(health-loop): HealthLoopConfig ORM model"
```

---

### Task 2: alembic migration 055

**Files:** Create `migrations/versions/055_health_loop_config.py`. Test: `tests/test_health_loop_config.py`.

- [ ] **Step 1: Write the failing test (migration imports + chains off 054)**

Append to `tests/test_health_loop_config.py`:

```python
import importlib.util
from pathlib import Path


def _load_migration():
    path = Path("migrations/versions/055_health_loop_config.py")
    spec = importlib.util.spec_from_file_location("m055", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_055_chains_off_054_and_defines_up_down():
    m = _load_migration()
    assert m.revision == "055"
    assert m.down_revision == "054"
    assert callable(m.upgrade)
    assert callable(m.downgrade)
```

- [ ] **Step 2: Run the test — verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_config.py -k migration -q`
Expected: FAIL — `FileNotFoundError` / spec load error (file doesn't exist)

- [ ] **Step 3: Implement the migration**

Create `migrations/versions/055_health_loop_config.py`:

```python
"""health_loop_config

Revision ID: 055
Revises: 054
Create Date: 2026-06-09

Creates health_loop_configs — per-repo auto-heal loop settings (ADR /
docs/superpowers/specs/2026-06-09-auto-heal-loop-design.md). One row per
repo; repo_id PK (1:1). suppressed_finding_hashes is the won't-fix list.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "055"
down_revision = "054"


def upgrade() -> None:
    op.create_table(
        "health_loop_configs",
        sa.Column(
            "repo_id",
            sa.Integer(),
            sa.ForeignKey("repos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "cleanup_branch",
            sa.String(length=255),
            nullable=False,
            server_default="auto-agent/health-cleanup",
        ),
        sa.Column("batch_size", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="idle"),
        sa.Column(
            "suppressed_finding_hashes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "supervisor_task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("health_loop_configs")
```

- [ ] **Step 4: Run the test — verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_config.py -k migration -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/055_health_loop_config.py tests/test_health_loop_config.py
git commit -m "feat(health-loop): migration 055 — health_loop_configs table"
```

---

### Task 3: config service (pure dedup helper + async CRUD)

**Files:** Create `agent/health_loop/config_service.py`. Test: `tests/test_health_loop_config.py`.

- [ ] **Step 1: Write the failing test (the pure dedup helper)**

Append to `tests/test_health_loop_config.py`:

```python
def test_dedup_append_adds_once_and_preserves_order():
    from agent.health_loop.config_service import _dedup_append

    assert _dedup_append(["a", "b"], "c") == ["a", "b", "c"]
    # Already present ⇒ unchanged (no duplicate).
    assert _dedup_append(["a", "b"], "a") == ["a", "b"]
    # Empty start.
    assert _dedup_append([], "x") == ["x"]
```

- [ ] **Step 2: Run the test — verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_config.py -k dedup -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.health_loop.config_service'`

- [ ] **Step 3: Implement the service**

First, READ an existing get-or-create config accessor to mirror the session pattern — e.g. `agent/lifecycle/_orchestrator_api.py::get_freeform_config` or how `RepoGraphConfig` is fetched (`grep -rn "RepoGraphConfig" agent/ orchestrator/ | grep -i select`). Match how the repo opens an `async_session` and commits.

Create `agent/health_loop/config_service.py`:

```python
"""Async CRUD for HealthLoopConfig (per-repo auto-heal loop settings).

DB access from the agent layer is allowed (agent → shared), mirroring
agent/po_analyzer.py. All writes commit within an ``async_session`` block.
"""
from __future__ import annotations

from sqlalchemy import select

from shared.database import async_session
from shared.models import HealthLoopConfig


def _dedup_append(items: list[str], value: str) -> list[str]:
    """Append ``value`` once, preserving order. Pure."""
    return items if value in items else [*items, value]


async def get_config(repo_id: int) -> HealthLoopConfig | None:
    """Return the loop config for ``repo_id``, or None if not configured."""
    async with async_session() as session:
        result = await session.execute(
            select(HealthLoopConfig).where(HealthLoopConfig.repo_id == repo_id)
        )
        return result.scalar_one_or_none()


async def get_or_create_config(repo_id: int, organization_id: int) -> HealthLoopConfig:
    """Return the config, creating a default (disabled) row if absent."""
    async with async_session() as session:
        result = await session.execute(
            select(HealthLoopConfig).where(HealthLoopConfig.repo_id == repo_id)
        )
        cfg = result.scalar_one_or_none()
        if cfg is None:
            cfg = HealthLoopConfig(repo_id=repo_id, organization_id=organization_id)
            session.add(cfg)
            await session.commit()
            await session.refresh(cfg)
        return cfg


async def set_enabled(repo_id: int, enabled: bool) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(HealthLoopConfig).where(HealthLoopConfig.repo_id == repo_id)
        )
        cfg = result.scalar_one_or_none()
        if cfg is not None:
            cfg.enabled = enabled
            await session.commit()


async def set_state(repo_id: int, state: str) -> None:
    """Set the supervisor lifecycle state (idle|running|paused)."""
    async with async_session() as session:
        result = await session.execute(
            select(HealthLoopConfig).where(HealthLoopConfig.repo_id == repo_id)
        )
        cfg = result.scalar_one_or_none()
        if cfg is not None:
            cfg.state = state
            await session.commit()


async def suppress_finding(repo_id: int, finding_hash: str) -> None:
    """Add ``finding_hash`` to the won't-fix list (idempotent)."""
    async with async_session() as session:
        result = await session.execute(
            select(HealthLoopConfig).where(HealthLoopConfig.repo_id == repo_id)
        )
        cfg = result.scalar_one_or_none()
        if cfg is not None:
            cfg.suppressed_finding_hashes = _dedup_append(
                list(cfg.suppressed_finding_hashes or []), finding_hash
            )
            await session.commit()


async def get_suppressed(repo_id: int) -> set[str]:
    """Return the suppressed finding-hash set (empty if unconfigured)."""
    cfg = await get_config(repo_id)
    return set(cfg.suppressed_finding_hashes or []) if cfg else set()
```

> Note on JSONB mutation: reassign a NEW list (as above) rather than mutating in place — SQLAlchemy only reliably detects reassignment on a plain JSONB column without `MutableList`. The `_dedup_append` helper returns a new list, which is why it's used.

- [ ] **Step 4: Run the test — verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_config.py -k dedup -q`
Expected: PASS

- [ ] **Step 5: Full file + lint**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_config.py -q`
Expected: PASS (columns + migration + dedup; any DB-CRUD tests, if added, SKIP without DATABASE_URL)
Run: `.venv/bin/ruff check agent/health_loop/config_service.py shared/models/core.py tests/test_health_loop_config.py`
Expected: `All checks passed!`
Run: `.venv/bin/ruff format --check agent/health_loop/config_service.py tests/test_health_loop_config.py migrations/versions/055_health_loop_config.py`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add agent/health_loop/config_service.py tests/test_health_loop_config.py
git commit -m "feat(health-loop): config service (get/create, enable, state, suppress)"
```

---

### Phase 5a exit criteria

- `HealthLoopConfig` model + export; `text`/`JSONB`/`_utcnow` imports satisfied.
- Migration `055` chains off `054`, creates `health_loop_configs`, imports cleanly.
- `config_service` exposes `get_config`, `get_or_create_config`, `set_enabled`,
  `set_state`, `suppress_finding`, `get_suppressed`, `_dedup_append`.
- Locally: model-structure + migration-chain + dedup tests pass; ruff clean.
  CRUD behavior is VM/CI-verified (no DB locally).
- Full suite shows no NEW failures (the model registers on `Base.metadata`
  without breaking existing model imports / autogenerate).
```
