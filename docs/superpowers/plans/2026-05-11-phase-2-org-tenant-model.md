# Phase 2 — Org/Tenant Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make auto-agent multi-tenant. Every row that represents customer data gains an `organization_id`. Every user-facing query filters by the requester's active org. Two orgs cannot see each other's anything. Users can belong to many orgs and switch between them.

**Architecture:**
An `organizations` table is the tenant. A `organization_memberships` join table makes users↔orgs many-to-many (per the user's M2M decision). The JWT carries both `user_id` and `current_org_id`; an org-switcher endpoint re-issues the JWT with a different `current_org_id`. Every scope-able model gains `organization_id` (Task, Repo, Suggestion, FreeformConfig, ScheduledTask, SearchSession, plus UserSecret which becomes per-(user, org)). A `scoped()` query helper appends the org filter; a parametrized isolation test asserts user A in org A cannot see anything in org B via any endpoint. Migration 026 adds nullable columns + backfills default org. Migration 027 flips NOT NULL and adds indexes after backfill is validated. Background pollers stay process-level (Phase 4 introduces per-org concurrency); WebSocket broadcasts gain an `org_id` filter at delivery time.

**Tech Stack:** PostgreSQL 16 + pgcrypto, Alembic, SQLAlchemy 2.0 (async), FastAPI, PyJWT, pytest-asyncio, Next.js 14 (App Router), TanStack Query.

**Companion docs:**
- Original spec: `docs/superpowers/plans/2026-05-09-multi-tenant-saas-implementation.md` (Phase 2 section)
- Handover that triggered this work: `docs/superpowers/plans/2026-05-11-handover-after-phase-1.md`

**Open-from-Phase-1 caveats acknowledged:**
- Step-1 (real signup walkthrough) and step-2 (per-user PAT in prod) from the handover are NOT yet verified. The user accepted the risk of starting Phase 2 anyway. If you hit a Phase 1 bug masquerading as Phase 2, the diff for this branch is the suspect.

**PR shape:** One bundled PR (Phase 1 precedent). Multiple small commits inside the PR — one per task block below. **Don't squash on merge.** The migration commits MUST stay separate so a future rollback can revert just the migration.

---

## Glossary

- **Org / Organization / Tenant**: All synonyms for the same row in `organizations`.
- **Membership**: A row in `organization_memberships` tying one user to one org with a role.
- **Active org**: The org_id the user is currently "operating as" — encoded in their JWT as `current_org_id`. A user with N memberships has one active org at a time; switching = re-issuing the JWT.
- **Scoped query**: A SQLAlchemy `select(...)` that has been passed through `scoped()` and now includes `WHERE model.organization_id = current_org_id`.
- **Direct-scoped model**: Model with its own `organization_id` column (Task, Repo, Suggestion, FreeformConfig, ScheduledTask, SearchSession, UserSecret).
- **Transitively-scoped model**: Model that lives under a direct-scoped parent (TaskHistory, TaskMessage, TaskOutcome via Task; SearchMessage via SearchSession). The scoping helper joins to the parent.

---

## Membership model — design locked in

```python
class OrganizationMembership(Base):
    __tablename__ = "organization_memberships"
    org_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role = Column(String(32), nullable=False, default="member")  # "owner" | "admin" | "member"
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
```

**Roles (this phase):**
- `owner` — exactly one per org (the creator). Cannot be removed. Can transfer ownership.
- `admin` — manage members, integrations, billing. (Billing lands in Phase 5; field reserved.)
- `member` — create/run tasks; read repos/suggestions; cannot manage members.

**Resolution rules:**
- On signup, a personal org is created with the new user as `owner`. Org `name` = display_name, `slug` = lowercased username + random 4-char suffix.
- On `verify_email`, the org is already created (signup did it). Verification just flips the user's `email_verified_at`.
- On login, `current_org_id` = the user's most-recently-active membership (track via a `last_active_at` column on the membership; falls back to oldest membership).
- Legacy users (pre-Phase-2 rows) get added to a default org `slug='default'` with role `owner` in migration 026.

**Invitation flow (this phase):**
- `POST /api/orgs/{org_id}/members { email, role }` (admin/owner only).
- If `email` matches an existing verified user → create membership row, send notification email, done.
- If `email` is new → return 404 with body `{ "error": "no_such_user", "hint": "Ask them to sign up at /signup first" }`. Cross-org invites for non-existent users are deferred (they need an email click-through workflow which is Phase 6 work).

---

## File Structure

### New files
- `migrations/versions/026_organizations.py` — orgs + memberships + nullable org_id on all scope tables + backfill
- `migrations/versions/027_organizations_not_null.py` — flip NOT NULL + indexes + repo (org_id, name) uniqueness
- `orchestrator/scoping.py` — `current_org_id()` dependency + `scoped()` query helper + transitive-scope helpers
- `orchestrator/orgs.py` — member management endpoints (kept out of `router.py` to avoid bloating it past its 1900-line ceiling)
- `tests/conftest.py` — extend with `two_orgs`, `user_in_org_a`, etc. fixtures (modification not new file)
- `tests/test_org_isolation.py` — parametrized cross-org isolation test
- `tests/test_scoping.py` — unit tests for the `scoped()` helper
- `tests/test_org_membership.py` — member CRUD endpoint tests
- `tests/test_org_switcher.py` — `POST /api/me/current-org` behavior
- `web-next/components/org-switcher.tsx` — header dropdown
- `web-next/app/(app)/settings/organization/page.tsx` — org settings + members table
- `web-next/lib/orgs.ts` — fetch helpers for org endpoints
- `web-next/hooks/use-current-org.ts` — TanStack Query hook returning `{ current, all }`

### Modified files
- `shared/models.py` — add `Organization`, `OrganizationMembership`; add `organization_id` Column to scope models
- `orchestrator/auth.py` — `create_token()` gains `current_org_id` param; new `current_org_id()` FastAPI dep
- `orchestrator/router.py` — every list/get/mutate endpoint wraps queries in `scoped()`; signup creates personal org; login resolves active org
- `agent/po_analyzer.py`, `agent/architect_analyzer.py` — propagate `organization_id` when creating Suggestion rows
- `orchestrator/queue.py`, `orchestrator/metrics.py`, `orchestrator/deduplicator.py`, `orchestrator/feedback.py`, `orchestrator/search.py`, `orchestrator/repo_sync.py` — scope queries by org where the call site has one
- `run.py` — pollers don't change query shape (they iterate all tasks process-wide) but pass `organization_id` to downstream notifiers
- `web/main.py` — `_broadcast()` accepts `org_id`; per-WS-connection client tracks `current_org_id`; delivery skips clients in other orgs
- `web-next/app/(app)/layout.tsx` — render `<OrgSwitcher>` in header

---

# Task 1: Create migration 026 — orgs, memberships, nullable org_id columns

**Files:**
- Create: `migrations/versions/026_organizations.py`
- Test: `tests/test_migration_026.py` (new — light validation, mostly we trust alembic upgrade)

- [ ] **Step 1: Write the failing migration validation test**

```python
# tests/test_migration_026.py
"""Migration 026 — orgs + memberships + scoped org_id columns.

The real validation is `alembic upgrade head` against a clean DB succeeding.
This test asserts the schema is what we expect afterwards.
"""

import pytest
from sqlalchemy import inspect, text
from shared.database import async_session


@pytest.mark.asyncio
async def test_organizations_table_exists():
    async with async_session() as s:
        result = await s.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='organizations' ORDER BY column_name"
        ))
        cols = {row[0] for row in result.all()}
        assert cols >= {"id", "name", "slug", "created_at"}


@pytest.mark.asyncio
async def test_default_org_seeded():
    async with async_session() as s:
        result = await s.execute(text("SELECT slug FROM organizations WHERE slug='default'"))
        assert result.scalar_one_or_none() == "default"


@pytest.mark.asyncio
async def test_org_id_columns_present_on_scoped_tables():
    expected_tables = [
        "users", "repos", "tasks", "scheduled_tasks", "suggestions",
        "freeform_configs", "search_sessions", "user_secrets",
    ]
    async with async_session() as s:
        for table in expected_tables:
            result = await s.execute(text(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_name='{table}' AND column_name='organization_id'"
            ))
            assert result.scalar_one_or_none() == "organization_id", \
                f"organization_id missing from {table}"


@pytest.mark.asyncio
async def test_organization_memberships_table_exists():
    async with async_session() as s:
        result = await s.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='organization_memberships' ORDER BY column_name"
        ))
        cols = {row[0] for row in result.all()}
        assert cols >= {"org_id", "user_id", "role", "created_at"}


@pytest.mark.asyncio
async def test_existing_users_backfilled_to_default_org():
    """Every pre-migration user must have a membership in the default org."""
    async with async_session() as s:
        result = await s.execute(text("""
            SELECT COUNT(*) FROM users u
            WHERE NOT EXISTS (
                SELECT 1 FROM organization_memberships m
                WHERE m.user_id = u.id
            )
        """))
        assert result.scalar_one() == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_migration_026.py -v
```
Expected: FAIL (`relation "organizations" does not exist`).

- [ ] **Step 3: Write the migration**

```python
# migrations/versions/026_organizations.py
"""Phase 2 — Organizations and memberships.

Adds the multi-tenant tenant boundary. Every row representing customer data
gains a nullable ``organization_id`` FK that points at ``organizations``.
This migration backfills every existing row to a single ``default`` org;
migration 027 flips the columns to NOT NULL after backfill is verified
on production.

Users<->orgs is many-to-many via ``organization_memberships``. Every
pre-existing user becomes the owner of the default org (in practice the
default org has one owner — the legacy admin — and other legacy users
end up as members for safety).
"""

from typing import Sequence, Union

from alembic import op


revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SCOPED_TABLES = [
    "users", "repos", "tasks", "scheduled_tasks",
    "suggestions", "freeform_configs", "search_sessions",
    "user_secrets",
]


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS organizations (
            id          SERIAL PRIMARY KEY,
            name        VARCHAR(255) NOT NULL,
            slug        VARCHAR(64) NOT NULL UNIQUE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS organization_memberships (
            org_id           INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role             VARCHAR(32) NOT NULL DEFAULT 'member',
            last_active_at   TIMESTAMPTZ NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (org_id, user_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_memberships_user ON organization_memberships(user_id)")

    op.execute("""
        INSERT INTO organizations (name, slug)
        VALUES ('Default', 'default')
        ON CONFLICT (slug) DO NOTHING
    """)

    for table in SCOPED_TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS organization_id INTEGER "
            f"REFERENCES organizations(id)"
        )
        op.execute(
            f"UPDATE {table} SET organization_id = "
            f"(SELECT id FROM organizations WHERE slug='default') "
            f"WHERE organization_id IS NULL"
        )

    op.execute("""
        INSERT INTO organization_memberships (org_id, user_id, role)
        SELECT (SELECT id FROM organizations WHERE slug='default'), u.id,
               CASE WHEN u.id = 1 THEN 'owner' ELSE 'member' END
        FROM users u
        ON CONFLICT (org_id, user_id) DO NOTHING
    """)


def downgrade() -> None:
    for table in reversed(SCOPED_TABLES):
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS organization_id")

    op.execute("DROP INDEX IF EXISTS ix_memberships_user")
    op.execute("DROP TABLE IF EXISTS organization_memberships")
    op.execute("DROP TABLE IF EXISTS organizations")
```

- [ ] **Step 4: Run the migration**

```bash
docker compose exec auto-agent alembic upgrade head
# Or for host venv against Mac-native Postgres:
.venv/bin/python3 -m alembic upgrade head
```
Expected output ends with: `INFO  [alembic.runtime.migration] Running upgrade 025 -> 026, Phase 2 — Organizations and memberships`

- [ ] **Step 5: Run validation tests**

```bash
.venv/bin/python3 -m pytest tests/test_migration_026.py -v
```
Expected: 5 passes.

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/026_organizations.py tests/test_migration_026.py
git commit -m "feat(migration): 026 organizations + memberships + nullable org_id"
```

---

# Task 2: Add ORM models for Organization, OrganizationMembership, and `organization_id` columns on scoped models

**Files:**
- Modify: `shared/models.py` (whole file, but specifically adds two new classes and a `Column` to ~8 existing classes)
- Test: `tests/test_models_orgs.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models_orgs.py
"""ORM model wiring for Phase 2 — orgs + memberships."""

import pytest
from sqlalchemy import select

from shared.database import async_session
from shared.models import (
    Organization, OrganizationMembership, Repo, Task,
    Suggestion, FreeformConfig, ScheduledTask, SearchSession, User, UserSecret,
)


def test_organization_model_exists():
    assert hasattr(Organization, "id")
    assert hasattr(Organization, "name")
    assert hasattr(Organization, "slug")


def test_membership_model_composite_pk():
    cols = {c.name for c in OrganizationMembership.__table__.primary_key.columns}
    assert cols == {"org_id", "user_id"}


@pytest.mark.parametrize("model", [
    Repo, Task, Suggestion, FreeformConfig, ScheduledTask,
    SearchSession, UserSecret, User,
])
def test_scoped_model_has_org_id(model):
    assert hasattr(model, "organization_id"), f"{model.__name__} missing organization_id"


@pytest.mark.asyncio
async def test_can_load_default_org():
    async with async_session() as s:
        result = await s.execute(select(Organization).where(Organization.slug == "default"))
        assert result.scalar_one().name == "Default"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_models_orgs.py -v
```
Expected: FAIL with `ImportError: cannot import name 'Organization'`.

- [ ] **Step 3: Add the ORM classes and columns**

In `shared/models.py`:

```python
# After the existing enums, before `class Repo`:
class Organization(Base):
    """A tenant. Every customer-facing row belongs to exactly one org."""
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class OrganizationMembership(Base):
    """User<->org join. Composite PK (org_id, user_id)."""
    __tablename__ = "organization_memberships"

    org_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True, nullable=False,
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True, nullable=False,
    )
    role = Column(String(32), nullable=False, default="member")  # owner|admin|member
    last_active_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
```

Add `organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)` to each of:
- `Repo` (after `created_at`, around line 79)
- `Task` (after `created_at`, around line 111)
- `ScheduledTask` (after `created_at`, around line 172)
- `Suggestion` (after `repo_id`, around line 180)
- `FreeformConfig` (after `repo_id`, around line 216)
- `SearchSession` (after `user_id`, around line 299)
- `UserSecret` (after `key`, around line 286)
- `User` (after `signup_token`, around line 274)

Keep `nullable=True` for this commit. Migration 027 flips it.

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python3 -m pytest tests/test_models_orgs.py -v
```
Expected: 11 passes (parametrized over 8 scoped models + 3 standalone).

- [ ] **Step 5: Smoke the whole test suite**

```bash
.venv/bin/python3 -m pytest tests/ -q
```
Expected: 732 + new = ~743 pass, 0 fail.

- [ ] **Step 6: Commit**

```bash
git add shared/models.py tests/test_models_orgs.py
git commit -m "feat(models): Organization, OrganizationMembership, organization_id on scoped models"
```

---

# Task 3: Two-orgs test fixture in conftest

**Files:**
- Modify: `tests/conftest.py` (add fixtures; keep existing autouse fixtures intact)
- Test: implicitly tested by everything that uses these fixtures, plus a sanity test

- [ ] **Step 1: Write the failing sanity test**

```python
# tests/test_two_orgs_fixture.py
"""Sanity-check that the two_orgs fixture produces the expected isolation context."""

import pytest


@pytest.mark.asyncio
async def test_two_orgs_fixture_produces_two_orgs(two_orgs):
    assert two_orgs.org_a.id != two_orgs.org_b.id
    assert two_orgs.user_a.id != two_orgs.user_b.id
    assert two_orgs.repo_a.organization_id == two_orgs.org_a.id
    assert two_orgs.repo_b.organization_id == two_orgs.org_b.id
    assert two_orgs.task_a.organization_id == two_orgs.org_a.id
    assert two_orgs.task_b.organization_id == two_orgs.org_b.id


@pytest.mark.asyncio
async def test_two_orgs_users_belong_to_their_org_only(two_orgs):
    # user_a is a member of org_a only
    member_orgs = {m.org_id for m in two_orgs.user_a_memberships}
    assert member_orgs == {two_orgs.org_a.id}


@pytest.mark.asyncio
async def test_two_orgs_tokens_carry_current_org(two_orgs):
    import jwt
    from orchestrator.auth import JWT_SECRET
    payload_a = jwt.decode(two_orgs.token_a, JWT_SECRET, algorithms=["HS256"])
    assert payload_a["user_id"] == two_orgs.user_a.id
    assert payload_a["current_org_id"] == two_orgs.org_a.id
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_two_orgs_fixture.py -v
```
Expected: FAIL with `fixture 'two_orgs' not found`.

- [ ] **Step 3: Add the fixture**

Append to `tests/conftest.py`:

```python
from dataclasses import dataclass

from shared.database import async_session
from shared.models import (
    Organization, OrganizationMembership, Repo, Task, TaskSource, User,
)


@dataclass
class TwoOrgs:
    org_a: Organization
    org_b: Organization
    user_a: User
    user_b: User
    repo_a: Repo
    repo_b: Repo
    task_a: Task
    task_b: Task
    token_a: str
    token_b: str
    user_a_memberships: list[OrganizationMembership]
    user_b_memberships: list[OrganizationMembership]


@pytest.fixture
async def two_orgs() -> TwoOrgs:
    """Create two fully-isolated orgs with one user, repo, and task each.

    Use in any test that needs to assert cross-org isolation. The whole point
    of this fixture is to make 'user_a cannot see user_b's data' a one-liner.
    """
    from orchestrator.auth import create_token, hash_password

    async with async_session() as s:
        org_a = Organization(name="Org A", slug=f"org-a-{id(s)}")
        org_b = Organization(name="Org B", slug=f"org-b-{id(s)}")
        s.add_all([org_a, org_b])
        await s.flush()

        user_a = User(username=f"alice-{org_a.id}", display_name="Alice",
                      password_hash=hash_password("pw"))
        user_b = User(username=f"bob-{org_b.id}", display_name="Bob",
                      password_hash=hash_password("pw"))
        s.add_all([user_a, user_b])
        await s.flush()

        m_a = OrganizationMembership(org_id=org_a.id, user_id=user_a.id, role="owner")
        m_b = OrganizationMembership(org_id=org_b.id, user_id=user_b.id, role="owner")
        s.add_all([m_a, m_b])

        repo_a = Repo(name=f"repo-a-{org_a.id}", url="git@example.com:a/a.git",
                      organization_id=org_a.id)
        repo_b = Repo(name=f"repo-b-{org_b.id}", url="git@example.com:b/b.git",
                      organization_id=org_b.id)
        s.add_all([repo_a, repo_b])
        await s.flush()

        task_a = Task(title="Task A", source=TaskSource.MANUAL,
                      organization_id=org_a.id, repo_id=repo_a.id,
                      created_by_user_id=user_a.id)
        task_b = Task(title="Task B", source=TaskSource.MANUAL,
                      organization_id=org_b.id, repo_id=repo_b.id,
                      created_by_user_id=user_b.id)
        s.add_all([task_a, task_b])
        await s.commit()
        await s.refresh(task_a); await s.refresh(task_b)
        await s.refresh(org_a);  await s.refresh(org_b)
        await s.refresh(user_a); await s.refresh(user_b)
        await s.refresh(repo_a); await s.refresh(repo_b)

        token_a = create_token(user_a.id, user_a.username, current_org_id=org_a.id)
        token_b = create_token(user_b.id, user_b.username, current_org_id=org_b.id)

        yield TwoOrgs(
            org_a=org_a, org_b=org_b,
            user_a=user_a, user_b=user_b,
            repo_a=repo_a, repo_b=repo_b,
            task_a=task_a, task_b=task_b,
            token_a=token_a, token_b=token_b,
            user_a_memberships=[m_a],
            user_b_memberships=[m_b],
        )

        # Teardown — delete in reverse FK order.
        for obj in (task_a, task_b, repo_a, repo_b, m_a, m_b,
                    user_a, user_b, org_a, org_b):
            await s.delete(obj)
        await s.commit()
```

> `create_token` doesn't yet accept `current_org_id` — Task 4 adds it. The fixture test stays red until then.

- [ ] **Step 4: Verify fixture imports without crashing**

```bash
.venv/bin/python3 -c "from tests.conftest import two_orgs; print('ok')"
```
Expected: `ok` (the import works; the fixture body will fail at runtime until Task 4).

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/test_two_orgs_fixture.py
git commit -m "test(fixtures): two_orgs fixture for cross-org isolation assertions"
```

> Do not run the fixture tests yet — they're red until Task 4. We commit the test now so the next task's red→green loop is meaningful.

---

# Task 4: JWT carries `current_org_id`; add `current_org_id()` dependency

**Files:**
- Modify: `orchestrator/auth.py:28-71`
- Test: `tests/test_auth_org.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth_org.py
"""JWT and FastAPI deps carry current_org_id after Phase 2."""

import jwt
from fastapi import HTTPException
import pytest

from orchestrator.auth import (
    JWT_SECRET, COOKIE_NAME, create_token, current_org_id, verify_token,
)


def test_create_token_includes_current_org_id():
    token = create_token(user_id=1, username="alice", current_org_id=42)
    payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    assert payload["current_org_id"] == 42


def test_create_token_current_org_id_required():
    """current_org_id is a required positional/keyword arg.

    Legacy callers that pre-date Phase 2 will fail loudly rather than silently
    issuing a token with no org context.
    """
    with pytest.raises(TypeError):
        create_token(user_id=1, username="alice")  # type: ignore[call-arg]


def test_current_org_id_dep_returns_org_from_cookie():
    token = create_token(1, "alice", current_org_id=7)
    assert current_org_id(authorization=None, auto_agent_session=token) == 7


def test_current_org_id_dep_raises_when_missing():
    """A pre-Phase-2 token without current_org_id is rejected (forces re-login)."""
    legacy_token = jwt.encode({"user_id": 1, "username": "alice", "exp": 2**31},
                              JWT_SECRET, algorithm="HS256")
    with pytest.raises(HTTPException) as e:
        current_org_id(authorization=None, auto_agent_session=legacy_token)
    assert e.value.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_auth_org.py -v
```
Expected: FAIL (`create_token() got an unexpected keyword argument 'current_org_id'`).

- [ ] **Step 3: Update `orchestrator/auth.py`**

Replace the existing `create_token` and add `current_org_id`:

```python
def create_token(
    user_id: int,
    username: str,
    *,
    current_org_id: int,
    expires_seconds: int = DEFAULT_EXPIRY,
) -> str:
    """Create a JWT token.

    ``current_org_id`` is required — every authenticated request must know
    which tenant it's operating against. Callers that pre-date Phase 2 will
    fail at this signature change, which is intentional: legacy paths are
    a security risk.
    """
    payload = {
        "user_id": user_id,
        "username": username,
        "current_org_id": current_org_id,
        "exp": int(time.time()) + expires_seconds,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def current_org_id(
    authorization: str | None = Header(None),
    auto_agent_session: str | None = Cookie(default=None),
) -> int:
    """FastAPI dependency that extracts the authenticated user's active org_id.

    Raises 401 if the token has no ``current_org_id`` — that means the token
    was issued by pre-Phase-2 code and the user must re-authenticate.
    """
    payload = verify_cookie_or_header(auto_agent_session, authorization)
    org_id = payload.get("current_org_id")
    if org_id is None:
        raise HTTPException(status_code=401, detail="Session predates org model — please log in again")
    return org_id
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python3 -m pytest tests/test_auth_org.py tests/test_two_orgs_fixture.py -v
```
Expected: 4 + 3 = 7 passes.

- [ ] **Step 5: Smoke whole suite (expecting many failures — that's fine for now)**

```bash
.venv/bin/python3 -m pytest tests/ -q 2>&1 | tail -20
```
Expected: A wave of failures from existing tests calling `create_token(user_id, username)` without `current_org_id`. These will all be fixed in the next sub-step.

- [ ] **Step 6: Fix every `create_token` callsite to pass `current_org_id`**

Find them with:
```bash
grep -rn "create_token(" --include="*.py" .
```

Expected callsites (verify with grep):
- `orchestrator/router.py:237` — login: pass `current_org_id=<resolved_org>` (see Task 6)
- `orchestrator/router.py:~395` — signup: pass `current_org_id=<new_personal_org_id>` (see Task 6)
- `orchestrator/router.py:~425` — verify_email: re-issue with org
- Any test that mints tokens directly (search `tests/` too)

Defer the router-side changes to Task 6 (they belong with the org-bootstrap logic). For tests, fix them now: pass `current_org_id=1` (or the test's org). Run:

```bash
grep -rn "create_token(" tests/ --include="*.py"
```

Update each callsite to add `current_org_id=<some_org_id>`. For tests that don't care about org isolation, `current_org_id=1` is fine.

- [ ] **Step 7: Re-run test suite**

```bash
.venv/bin/python3 -m pytest tests/ -q 2>&1 | tail -10
```
Expected: All previously-passing tests pass again; the router-side login/signup tests are still failing (they test pre-Phase-2 behavior — Task 6 fixes them).

- [ ] **Step 8: Commit**

```bash
git add orchestrator/auth.py tests/ -A
git commit -m "feat(auth): JWT carries current_org_id; current_org_id() FastAPI dependency"
```

---

# Task 5: Scoping helper — `orchestrator/scoping.py`

**Files:**
- Create: `orchestrator/scoping.py`
- Test: `tests/test_scoping.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scoping.py
"""Unit tests for the scoped() query helper."""

from sqlalchemy import select

from orchestrator.scoping import scoped
from shared.models import (
    Repo, Task, TaskHistory, TaskMessage, Suggestion,
    FreeformConfig, ScheduledTask, SearchSession, SearchMessage, UserSecret,
)


def _compile(query):
    return str(query.compile(compile_kwargs={"literal_binds": True}))


def test_scoped_direct_model_adds_org_filter():
    q = scoped(select(Repo), Repo, org_id=42)
    sql = _compile(q)
    assert "repos.organization_id = 42" in sql


def test_scoped_task_filters_by_task_org_id():
    q = scoped(select(Task), Task, org_id=42)
    sql = _compile(q)
    assert "tasks.organization_id = 42" in sql


def test_scoped_task_history_joins_through_task():
    q = scoped(select(TaskHistory), TaskHistory, org_id=42)
    sql = _compile(q)
    assert "task_history" in sql
    assert "tasks.organization_id = 42" in sql


def test_scoped_search_message_joins_through_session():
    q = scoped(select(SearchMessage), SearchMessage, org_id=42)
    sql = _compile(q)
    assert "search_sessions.organization_id = 42" in sql


def test_scoped_user_secret_filters_by_user_and_org():
    """UserSecret has both user_id and organization_id in its PK after migration."""
    q = scoped(select(UserSecret), UserSecret, org_id=42)
    sql = _compile(q)
    assert "user_secrets.organization_id = 42" in sql


def test_scoped_rejects_unknown_model():
    """Models not registered in scoping rules raise — better to fail loud than
    silently return un-scoped data."""
    class Bogus:
        pass
    import pytest
    with pytest.raises(KeyError):
        scoped(select(Bogus), Bogus, org_id=42)  # type: ignore[arg-type]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_scoping.py -v
```
Expected: FAIL (`ModuleNotFoundError: No module named 'orchestrator.scoping'`).

- [ ] **Step 3: Write the scoping helper**

```python
# orchestrator/scoping.py
"""Query scoping helper for the multi-tenant model.

Every query that returns customer-visible rows must be passed through
``scoped()`` so it filters by the requester's active organization. Models
fall into two buckets:

* **Direct-scoped** — has its own ``organization_id`` column. Filter is a
  simple ``WHERE model.organization_id = :org_id``.
* **Transitively-scoped** — lives under a direct-scoped parent. Filter is a
  JOIN to the parent plus a WHERE on the parent's org_id.

A model not registered here will raise ``KeyError`` — this is intentional.
Silent fall-through is how multi-tenant data leaks happen.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select

from shared.models import (
    FreeformConfig, Organization, OrganizationMembership, Repo,
    ScheduledTask, SearchMessage, SearchSession, Suggestion,
    Task, TaskHistory, TaskMessage, TaskOutcome, User, UserSecret,
)


# Models that have their own ``organization_id`` column.
_DIRECT_SCOPED: dict[type, Any] = {
    Repo: Repo.organization_id,
    Task: Task.organization_id,
    Suggestion: Suggestion.organization_id,
    FreeformConfig: FreeformConfig.organization_id,
    ScheduledTask: ScheduledTask.organization_id,
    SearchSession: SearchSession.organization_id,
    UserSecret: UserSecret.organization_id,
}


# Models whose org membership lives on a parent row.
# Each entry maps child -> (parent_class, child.parent_fk, parent.org_id_col).
_TRANSITIVE_SCOPED: dict[type, tuple[type, Any, Any]] = {
    TaskHistory: (Task, TaskHistory.task_id, Task.organization_id),
    TaskMessage: (Task, TaskMessage.task_id, Task.organization_id),
    TaskOutcome: (Task, TaskOutcome.task_id, Task.organization_id),
    SearchMessage: (SearchSession, SearchMessage.session_id, SearchSession.organization_id),
}


def scoped(query: Select, model: type, *, org_id: int) -> Select:
    """Append a WHERE clause that restricts ``query`` to rows in ``org_id``.

    Raises ``KeyError`` if ``model`` is not registered as either directly
    or transitively scoped. This is by design: ``select(Foo).where(...)``
    without ``scoped(...)`` is forbidden for tenant data.
    """
    if model in _DIRECT_SCOPED:
        return query.where(_DIRECT_SCOPED[model] == org_id)

    if model in _TRANSITIVE_SCOPED:
        parent_cls, child_fk, parent_org_col = _TRANSITIVE_SCOPED[model]
        # JOIN parent ON child.parent_fk = parent.id AND parent.organization_id = :org_id
        return query.join(parent_cls, child_fk == parent_cls.id).where(parent_org_col == org_id)

    raise KeyError(
        f"{model.__name__} is not registered in scoping rules. "
        f"If it represents tenant data, add it to _DIRECT_SCOPED or _TRANSITIVE_SCOPED. "
        f"If it's an admin-only table, query it directly (no scoping) and audit the callsite."
    )


__all__ = ["scoped"]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python3 -m pytest tests/test_scoping.py -v
```
Expected: 6 passes.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/scoping.py tests/test_scoping.py
git commit -m "feat(scoping): scoped() helper for org-scoped queries"
```

---

# Task 6: Signup creates a personal org; login resolves active org

**Files:**
- Modify: `orchestrator/router.py:202-247` (login), `:358-406` (signup), `:408-439` (verify_email)
- Test: `tests/test_signup_flow.py` (extend existing)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_signup_flow.py`:

```python
@pytest.mark.asyncio
async def test_signup_creates_personal_org(api_client):
    resp = await api_client.post("/api/auth/signup", json={
        "email": "newbie@example.com",
        "password": "hunter2hunter2",
        "display_name": "New Bie",
    })
    assert resp.status_code == 201

    async with async_session() as s:
        user = (await s.execute(
            select(User).where(User.email == "newbie@example.com")
        )).scalar_one()

        memberships = (await s.execute(
            select(OrganizationMembership).where(OrganizationMembership.user_id == user.id)
        )).scalars().all()
        assert len(memberships) == 1
        assert memberships[0].role == "owner"

        org = (await s.execute(
            select(Organization).where(Organization.id == memberships[0].org_id)
        )).scalar_one()
        assert org.name == "New Bie"
        assert org.slug.startswith("newbie")


@pytest.mark.asyncio
async def test_login_picks_user_active_org(api_client, two_orgs):
    """A user with one membership logs in and their JWT has that org."""
    resp = await api_client.post("/api/auth/login", json={
        "username": two_orgs.user_a.username,
        "password": "pw",
    })
    assert resp.status_code == 200
    token = resp.json()["token"]

    import jwt
    from orchestrator.auth import JWT_SECRET
    payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    assert payload["current_org_id"] == two_orgs.org_a.id
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_signup_flow.py::test_signup_creates_personal_org -v
```
Expected: FAIL — no org row created.

- [ ] **Step 3: Update signup handler**

In `orchestrator/router.py::signup` (around line 358-406), after creating the User row, add:

```python
# Phase 2 — every signup creates a personal org with the user as owner.
import secrets as _stdlib_secrets
suffix = _stdlib_secrets.token_hex(2)
slug = (username_seed := payload.email.split("@")[0].lower())[:20] + "-" + suffix
new_org = Organization(name=payload.display_name, slug=slug)
session.add(new_org)
await session.flush()
session.add(OrganizationMembership(
    org_id=new_org.id, user_id=user.id, role="owner",
))
# Backfill organization_id on the user row itself so legacy callers that
# read users.organization_id (if any) keep working.
user.organization_id = new_org.id
await session.commit()
```

Update the line that emits the verification email — no change there.

In `orchestrator/router.py::login` (around line 237), replace the `create_token(...)` call with:

```python
# Resolve user's active org: most recently active membership, else oldest.
active = (await session.execute(
    select(OrganizationMembership)
    .where(OrganizationMembership.user_id == user.id)
    .order_by(
        OrganizationMembership.last_active_at.desc().nullslast(),
        OrganizationMembership.created_at.asc(),
    )
    .limit(1)
)).scalar_one_or_none()
if not active:
    raise HTTPException(403, "User has no organization memberships")
active.last_active_at = _utcnow()
await session.commit()

token = create_token(user.id, user.username, current_org_id=active.org_id)
```

In `orchestrator/router.py::verify_email` (around line 408), after marking `email_verified_at`, re-issue the cookie with the user's first membership:

```python
membership = (await session.execute(
    select(OrganizationMembership)
    .where(OrganizationMembership.user_id == user.id)
    .order_by(OrganizationMembership.created_at.asc())
    .limit(1)
)).scalar_one_or_none()
if not membership:
    raise HTTPException(500, "Verified user has no org — signup is broken")
new_token = create_token(user.id, user.username, current_org_id=membership.org_id)
response.set_cookie(COOKIE_NAME, new_token, httponly=True, samesite="lax")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python3 -m pytest tests/test_signup_flow.py -v
```
Expected: all green.

- [ ] **Step 5: Smoke whole suite**

```bash
.venv/bin/python3 -m pytest tests/ -q
```
Expected: 0 failures.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/router.py tests/test_signup_flow.py
git commit -m "feat(auth): signup creates personal org; login resolves active org"
```

---

# Task 7: Cross-org isolation parametrized test (write before scoping endpoints)

**Files:**
- Create: `tests/test_org_isolation.py`

**Why this comes BEFORE endpoint scoping:** This is the spec's deliverable for Phase 2. Writing it now turns every subsequent endpoint-scoping task into a clear red→green: scope the endpoint, watch its row in this parametrized test go from red to green.

- [ ] **Step 1: Write the parametrized isolation suite**

```python
# tests/test_org_isolation.py
"""Cross-org isolation — user_a (in org_a) cannot see anything in org_b.

This is the spec's load-bearing test. Every endpoint that returns or mutates
tenant data must appear in the parametrize list. Adding a new endpoint that
returns tenant data without also adding it here is a security regression and
will be caught in code review.

Conventions:
- ``404`` means "we don't tell you whether the resource exists or not". We
  prefer 404 over 403 because 403 leaks existence.
- ``[]`` means "list endpoints return an empty list, never B's rows".
"""

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path_template,assert_kind", [
    # tasks
    ("GET",  "/api/tasks",                                       "empty_list"),
    ("GET",  "/api/tasks/{task_b.id}",                           "404"),
    ("GET",  "/api/tasks/{task_b.id}/messages",                  "404"),
    ("GET",  "/api/tasks/{task_b.id}/history",                   "404"),
    ("POST", "/api/tasks/{task_b.id}/cancel",                    "404"),
    ("POST", "/api/tasks/{task_b.id}/transition",                "404"),
    ("POST", "/api/tasks/{task_b.id}/done",                      "404"),
    ("POST", "/api/tasks/{task_b.id}/approve",                   "404"),
    ("POST", "/api/tasks/{task_b.id}/messages",                  "404"),
    ("PATCH","/api/tasks/{task_b.id}/repo",                      "404"),
    ("PATCH","/api/tasks/{task_b.id}/branch",                    "404"),
    ("PATCH","/api/tasks/{task_b.id}/subtasks",                  "404"),
    ("PATCH","/api/tasks/{task_b.id}/intake_qa",                 "404"),
    ("POST", "/api/tasks/{task_b.id}/priority",                  "404"),
    ("POST", "/api/tasks/{task_b.id}/outcome",                   "404"),
    ("DELETE", "/api/tasks/{task_b.id}",                         "404"),
    # repos
    ("GET",  "/api/repos",                                       "empty_or_no_b"),
    ("PATCH","/api/repos/{repo_b.name}/branch",                  "404"),
    ("POST", "/api/repos/{repo_b.name}/refresh-ci",              "404"),
    ("POST", "/api/repos/{repo_b.id}/harness",                   "404"),
    ("POST", "/api/repos/{repo_b.name}/onboard",                 "404"),
    ("POST", "/api/repos/{repo_b.id}/summary",                   "404"),
    ("DELETE", "/api/repos/{repo_b.name}",                       "404"),
    # suggestions
    ("GET",  "/api/suggestions",                                 "empty_list"),
    ("POST", "/api/suggestions/{suggestion_b.id}/approve",       "404"),
    ("POST", "/api/suggestions/{suggestion_b.id}/reject",        "404"),
    # freeform
    ("GET",  "/api/freeform/config",                             "empty_list"),
    ("DELETE", "/api/freeform/config/{freeform_config_b.id}",    "404"),
    ("POST", "/api/freeform/analyze/{repo_b.name}",              "404"),
    ("POST", "/api/freeform/{task_b.id}/promote",                "404"),
    ("POST", "/api/freeform/{task_b.id}/revert",                 "404"),
    # schedules
    ("GET",  "/api/schedules",                                   "empty_list"),
    ("DELETE", "/api/schedules/{schedule_b.id}",                 "404"),
    ("POST", "/api/schedules/{schedule_b.id}/toggle",            "404"),
    # secrets — UserSecret is now org-scoped via (user_id, org_id, key)
    ("GET",  "/api/me/secrets",                                  "empty_list"),
    # feedback
    ("GET",  "/api/feedback/summary",                            "org_a_only"),
    ("GET",  "/api/feedback/patterns",                           "org_a_only"),
])
async def test_user_a_cannot_access_org_b_resources(
    method, path_template, assert_kind, api_client, two_orgs_with_extras,
):
    extras = two_orgs_with_extras
    # Build path by interpolating the b-side resources.
    path = path_template.format(
        task_b=extras.task_b, repo_b=extras.repo_b,
        suggestion_b=extras.suggestion_b, freeform_config_b=extras.freeform_config_b,
        schedule_b=extras.schedule_b,
    )

    headers = {"Cookie": f"auto_agent_session={extras.token_a}"}
    body = {} if method in {"POST", "PATCH"} else None
    resp = await api_client.request(method, path, headers=headers, json=body)

    if assert_kind == "404":
        assert resp.status_code == 404, (
            f"{method} {path} returned {resp.status_code}; "
            f"expected 404 for cross-org access. Body: {resp.text[:200]}"
        )
    elif assert_kind == "empty_list":
        assert resp.status_code == 200, f"{method} {path}: {resp.status_code} {resp.text[:200]}"
        # Either {"data": []} or [] depending on endpoint. Be permissive.
        data = resp.json()
        if isinstance(data, dict):
            data = next(iter(data.values()), [])
        b_ids = {extras.task_b.id, extras.repo_b.id, extras.suggestion_b.id,
                 extras.freeform_config_b.id, extras.schedule_b.id}
        seen_ids = {r["id"] for r in data if isinstance(r, dict) and "id" in r}
        assert not (seen_ids & b_ids), \
            f"{method} {path} leaked org B row(s) {seen_ids & b_ids}"
    elif assert_kind == "empty_or_no_b":
        assert resp.status_code == 200
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("repos", data.get("data", []))
        assert extras.repo_b.name not in {r["name"] for r in data if "name" in r}
    elif assert_kind == "org_a_only":
        assert resp.status_code == 200
        # The body must not mention any task id that belongs to org_b. Tolerant check.
        assert str(extras.task_b.id) not in resp.text or \
            f'"id": {extras.task_b.id}' not in resp.text
    else:
        raise AssertionError(f"Unknown assert_kind: {assert_kind}")
```

The fixture `two_orgs_with_extras` extends `two_orgs` with a `Suggestion`, `FreeformConfig`, `ScheduledTask` in each org. Add to `tests/conftest.py`:

```python
@dataclass
class TwoOrgsWithExtras(TwoOrgs):
    suggestion_a: object
    suggestion_b: object
    freeform_config_a: object
    freeform_config_b: object
    schedule_a: object
    schedule_b: object


@pytest.fixture
async def two_orgs_with_extras(two_orgs) -> TwoOrgsWithExtras:
    from shared.models import Suggestion, FreeformConfig, ScheduledTask
    async with async_session() as s:
        sug_a = Suggestion(repo_id=two_orgs.repo_a.id, title="A1",
                           organization_id=two_orgs.org_a.id)
        sug_b = Suggestion(repo_id=two_orgs.repo_b.id, title="B1",
                           organization_id=two_orgs.org_b.id)
        ff_a = FreeformConfig(repo_id=two_orgs.repo_a.id, organization_id=two_orgs.org_a.id)
        ff_b = FreeformConfig(repo_id=two_orgs.repo_b.id, organization_id=two_orgs.org_b.id)
        sch_a = ScheduledTask(name=f"sched-a-{two_orgs.org_a.id}",
                              cron_expression="0 9 * * 1", task_title="X",
                              organization_id=two_orgs.org_a.id)
        sch_b = ScheduledTask(name=f"sched-b-{two_orgs.org_b.id}",
                              cron_expression="0 9 * * 1", task_title="Y",
                              organization_id=two_orgs.org_b.id)
        s.add_all([sug_a, sug_b, ff_a, ff_b, sch_a, sch_b])
        await s.commit()
        for o in (sug_a, sug_b, ff_a, ff_b, sch_a, sch_b):
            await s.refresh(o)
        yield TwoOrgsWithExtras(
            **vars(two_orgs),
            suggestion_a=sug_a, suggestion_b=sug_b,
            freeform_config_a=ff_a, freeform_config_b=ff_b,
            schedule_a=sch_a, schedule_b=sch_b,
        )
        for o in (sug_a, sug_b, ff_a, ff_b, sch_a, sch_b):
            await s.delete(o)
        await s.commit()
```

You also need an `api_client` fixture — likely already exists somewhere; if not, add a `httpx.AsyncClient(transport=ASGITransport(app=app))` fixture in `conftest.py`.

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_org_isolation.py -v 2>&1 | tail -30
```
Expected: ~36 parametrize cases, many fail (endpoints not yet scoped).

- [ ] **Step 3: Commit (red is fine — Tasks 8-12 turn it green)**

```bash
git add tests/test_org_isolation.py tests/conftest.py
git commit -m "test(org-isolation): parametrized cross-org access matrix (currently red)"
```

---

# Task 8: Scope all task endpoints in `orchestrator/router.py`

**Files:**
- Modify: `orchestrator/router.py` lines 651-1095 (every task endpoint)

**Pattern.** Every endpoint that takes `task_id` or returns Task rows gains a `current_org_id = Depends(current_org_id_dep)` parameter and wraps its query in `scoped(...)`.

- [ ] **Step 1: Import and rename**

At the top of `orchestrator/router.py` (around the existing imports), add:

```python
from orchestrator.auth import current_org_id as current_org_id_dep, current_user_id
from orchestrator.scoping import scoped
```

(If `current_user_id` is already imported as a name, keep it; the goal is both deps are available.)

- [ ] **Step 2: Update list_tasks (line 720-730)**

Replace:
```python
async def list_tasks(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Task).order_by(Task.created_at.desc()))
    ...
```

With:
```python
async def list_tasks(
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
):
    result = await session.execute(
        scoped(select(Task), Task, org_id=org_id).order_by(Task.created_at.desc())
    )
    ...
```

- [ ] **Step 3: Update get_task_detail (line 732-738) and every other `task_id`-receiving endpoint**

For each of:
- `get_task_detail`
- `delete_task`
- `set_task_priority`
- `cancel_task`
- `transition_task`
- `assign_repo` (PATCH /api/tasks/{task_id}/repo)
- `set_branch_name`
- `update_subtasks`
- `update_intake_qa`
- `list_task_messages`
- `post_task_message`
- `mark_task_done`
- `add_task_message`
- `approve_task`
- `record_task_outcome`
- `get_task_history`
- `promote_task` (freeform)
- `revert_task` (freeform)

Apply this pattern:

```python
async def get_task_detail(
    task_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
):
    task = (await session.execute(
        scoped(select(Task).where(Task.id == task_id), Task, org_id=org_id)
    )).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "Task not found")
    ...
```

The key change: previously a 404 was only raised if the task didn't exist; now 404 is raised if it doesn't exist OR belongs to a different org. Same response — no info leak.

For list-style endpoints (`list_task_messages`, `get_task_history`): scope the parent Task lookup first; if 404, return 404. The child fetch is by `task_id`, which is implicitly safe once the parent check passes.

- [ ] **Step 4: Update create_task (line 651-718)**

`create_task` needs to:
1. Verify the target repo (if `task.repo_id` is set) belongs to the requester's org.
2. Stamp the new task's `organization_id = current_org_id`.

```python
async def create_task(
    payload: TaskCreate,
    session: AsyncSession = Depends(get_session),
    user_id: int = Depends(current_user_id),
    org_id: int = Depends(current_org_id_dep),
):
    if payload.repo_id is not None:
        repo = (await session.execute(
            scoped(select(Repo).where(Repo.id == payload.repo_id), Repo, org_id=org_id)
        )).scalar_one_or_none()
        if repo is None:
            raise HTTPException(404, "Repo not found")
    ...
    task = Task(
        title=payload.title,
        # ... existing fields ...
        organization_id=org_id,
        created_by_user_id=user_id,
    )
    ...
```

- [ ] **Step 5: Run isolation test, scoped to task rows**

```bash
.venv/bin/python3 -m pytest tests/test_org_isolation.py -v -k task
```
Expected: all task-row parametrize cases pass.

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/python3 -m pytest tests/ -q
```
Expected: tasks-related isolation cases green. Other endpoints' isolation cases still red — that's fine, Tasks 9-12 fix them.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/router.py
git commit -m "feat(router): scope all task endpoints by current_org_id"
```

---

# Task 9: Scope all repo endpoints + repo (org_id, name) uniqueness

**Files:**
- Modify: `orchestrator/router.py:1164-1418` (all repo endpoints)

- [ ] **Step 1: Scope every repo endpoint**

Endpoints: `register_repo`, `list_repos`, `update_repo_branch`, `refresh_repo_ci_checks`, `update_repo_harness`, `trigger_harness_onboarding`, `update_repo_summary`, `delete_repo`.

Pattern (apply to each):

```python
async def list_repos(
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
):
    repos = (await session.execute(
        scoped(select(Repo), Repo, org_id=org_id).order_by(Repo.name)
    )).scalars().all()
    ...
```

For lookup-by-name (e.g. `update_repo_branch`), the lookup is `WHERE Repo.name == name AND Repo.organization_id == org_id` — handled by `scoped()`:

```python
repo = (await session.execute(
    scoped(select(Repo).where(Repo.name == name), Repo, org_id=org_id)
)).scalar_one_or_none()
if repo is None:
    raise HTTPException(404, "Repo not found")
```

For `register_repo`: stamp `organization_id = org_id` on the new Repo row.

For `delete_repo`: scope the lookup. The cascade delete of child tasks/suggestions is fine because the child rows already belong to the same org by construction.

- [ ] **Step 2: Run isolation test (repos)**

```bash
.venv/bin/python3 -m pytest tests/test_org_isolation.py -v -k repo
```
Expected: all repo cases green.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/router.py
git commit -m "feat(router): scope all repo endpoints by current_org_id"
```

---

# Task 10: Scope suggestions, freeform configs, scheduled tasks

**Files:**
- Modify: `orchestrator/router.py:1111-1162` (schedules), `:1420-1689` (freeform + suggestions)

- [ ] **Step 1: Scope each endpoint**

Apply the now-familiar pattern to:
- `create_schedule`, `list_schedules`, `delete_schedule`, `toggle_schedule`
- `upsert_freeform_config`, `list_freeform_configs`, `delete_freeform_config`, `create_repo_from_description`, `trigger_po_analysis`
- `list_suggestions`, `approve_suggestion`, `reject_suggestion`, `promote_task`, `revert_task`

For `create_repo_from_description`: stamp the new Repo, FreeformConfig, and Task all with `organization_id = org_id`.

For `upsert_freeform_config`: the lookup is by `repo_id`; first scope the parent Repo to confirm the user has access, then upsert.

- [ ] **Step 2: Run isolation test**

```bash
.venv/bin/python3 -m pytest tests/test_org_isolation.py -v
```
Expected: only the secrets and feedback rows still red.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/router.py
git commit -m "feat(router): scope suggestions, freeform, schedules by current_org_id"
```

---

# Task 11: Scope user_secrets (per-org), search sessions, feedback, claude pairing

**Files:**
- Modify: `migrations/versions/026_organizations.py` — drop and recreate user_secrets PK to include org_id (this is part of the same migration; we're amending it before the production deploy)
- Modify: `shared/secrets.py` — `set`/`get`/`delete`/`list_keys` take `org_id`
- Modify: `orchestrator/router.py:484-600` (secrets), `:1097-1109` (feedback), `:1830-1928` (claude pairing)
- Modify: `orchestrator/search.py:102-121`

> **Note on user_secrets:** The original Phase 2 spec just adds `organization_id` as a column. Because users now have M2M membership, each user can have *different* secrets per org (their personal GitHub PAT in org A, employer's PAT in org B). This requires changing the PK from `(user_id, key)` to `(user_id, organization_id, key)`. Migration 026 must be amended before production deploy.

- [ ] **Step 1: Amend migration 026 for user_secrets PK change**

In `migrations/versions/026_organizations.py::upgrade()`, after the existing `organization_id` column add, append:

```python
op.execute("""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'user_secrets_pkey') THEN
        ALTER TABLE user_secrets DROP CONSTRAINT user_secrets_pkey;
      END IF;
    END $$;
""")
op.execute("""
    UPDATE user_secrets us
    SET organization_id = (SELECT id FROM organizations WHERE slug='default')
    WHERE organization_id IS NULL
""")
op.execute("ALTER TABLE user_secrets ALTER COLUMN organization_id SET NOT NULL")
op.execute("""
    ALTER TABLE user_secrets ADD PRIMARY KEY (user_id, organization_id, key)
""")
```

Re-run the migration:
```bash
.venv/bin/python3 -m alembic downgrade 025
.venv/bin/python3 -m alembic upgrade 026
```

- [ ] **Step 2: Update `shared/secrets.py`**

Every function gains an `org_id: int` parameter:

```python
async def set(user_id: int, org_id: int, key: str, value: str) -> None:
    if key not in ALLOWED_KEYS:
        raise ValueError(f"Unknown secret key: {key}")
    async with async_session() as s:
        await s.execute(text("""
            INSERT INTO user_secrets (user_id, organization_id, key, value_enc, created_at, updated_at)
            VALUES (:uid, :oid, :k, pgp_sym_encrypt(:v, :p), now(), now())
            ON CONFLICT (user_id, organization_id, key) DO UPDATE SET
                value_enc = pgp_sym_encrypt(:v, :p), updated_at = now()
        """), {"uid": user_id, "oid": org_id, "k": key, "v": value, "p": _passphrase()})
        await s.commit()


async def get(user_id: int, org_id: int, key: str) -> str | None:
    async with async_session() as s:
        row = await s.execute(text("""
            SELECT pgp_sym_decrypt(value_enc, :p) FROM user_secrets
            WHERE user_id = :uid AND organization_id = :oid AND key = :k
        """), {"uid": user_id, "oid": org_id, "k": key, "p": _passphrase()})
        return row.scalar_one_or_none()


# delete() and list_keys() get the same org_id parameter; update SQL accordingly.
```

Update callsites:
```bash
grep -rn "secrets\.\(get\|set\|delete\|list_keys\)" --include="*.py" .
```

The big ones are in `orchestrator/router.py` (the 4 `/api/me/secrets` endpoints) and `shared/github_auth.py::get_github_token`. Both already have access to the calling user; they now also need `org_id`. For the router endpoints, take it from `current_org_id_dep`. For `get_github_token`, add an `org_id: int | None = None` parameter and thread it from callers.

- [ ] **Step 3: Update router endpoints for secrets**

```python
# /api/me/secrets — all 4 endpoints
async def list_my_secrets(
    user_id: int = Depends(current_user_id),
    org_id: int = Depends(current_org_id_dep),
):
    return await secrets.list_keys(user_id, org_id)

async def put_my_secret(
    key: str,
    payload: SecretValue,
    user_id: int = Depends(current_user_id),
    org_id: int = Depends(current_org_id_dep),
):
    if payload.value is None:
        await secrets.delete(user_id, org_id, key)
    else:
        await secrets.set(user_id, org_id, key, payload.value)
    return Response(status_code=204)

# similar for delete + test endpoints
```

- [ ] **Step 4: Update `shared/github_auth.py::get_github_token`**

Change signature to `get_github_token(*, user_id: int | None = None, org_id: int | None = None)`. The resolution order is now:

```python
async def get_github_token(*, user_id=None, org_id=None) -> str:
    # 1. Per-user PAT (scoped to the user's current org)
    if user_id is not None and org_id is not None:
        pat = await secrets.get(user_id, org_id, "github_pat")
        if pat:
            _log_mode("user_pat")
            return pat
    # 2. GitHub App installation token (org-level) — Phase 3 plugs this in
    # 3. Env PAT fallback
    if settings.github_token:
        _log_mode("env_pat")
        return settings.github_token
    raise NoGithubAuthAvailable()
```

All ~19 callsites that already pass `user_id` need to also pass `org_id`. Most have the Task in scope, so `org_id=task.organization_id`. Where the org isn't obvious (pollers): pass `org_id=None` and accept the env-PAT fallback.

- [ ] **Step 5: Scope search sessions**

In `orchestrator/search.py` (lines 102-103, 118-121): replace `where(SearchSession.user_id == user_id)` with `where(SearchSession.user_id == user_id, SearchSession.organization_id == org_id)`. Plumb `org_id` through the search router endpoint.

- [ ] **Step 6: Scope feedback endpoints**

`feedback_summary` and `feedback_patterns` query `TaskOutcome` and `TaskHistory`. Both are transitively-scoped via Task. Use `scoped(...)`.

- [ ] **Step 7: Scope claude pairing endpoints**

`_pair_start`, `_pair_code`, `_pair_status`, `_pair_disconnect` operate on `User` rows (claude_auth_status field). The User is the authenticated requester — these don't need org scoping per se, but the Claude credential vault path (`/data/users/{user_id}/.claude/`) is per-user. Pairing is **user-level**, not org-level. So: no change needed beyond confirming each endpoint uses `current_user_id` not anything else.

- [ ] **Step 8: Run isolation test in full**

```bash
.venv/bin/python3 -m pytest tests/test_org_isolation.py -v
```
Expected: all parametrize cases green.

- [ ] **Step 9: Run full suite**

```bash
.venv/bin/python3 -m pytest tests/ -q
```
Expected: 0 failures.

- [ ] **Step 10: Commit**

```bash
git add migrations/versions/026_organizations.py shared/secrets.py shared/github_auth.py orchestrator/router.py orchestrator/search.py
git commit -m "feat(scoping): user_secrets per-org, search/feedback/pair scoped"
```

---

# Task 12: Scope cross-module queries (agent + orchestrator internals)

**Files:**
- Modify: `agent/po_analyzer.py:57,107` — propagate `organization_id` on Suggestion creation; org-scope the Suggestion existence check
- Modify: `agent/architect_analyzer.py` — same pattern
- Modify: `orchestrator/queue.py` — `next_eligible_task()` scopes by org when called from a request context; pollers can call un-scoped variant
- Modify: `orchestrator/metrics.py` — scope all aggregations by org
- Modify: `orchestrator/deduplicator.py` — scope task lookups by org
- Modify: `orchestrator/repo_sync.py` — `Repo.name` lookup adds org filter
- Modify: `orchestrator/feedback.py` — scope all queries by org
- Modify: `run.py:1396-1689` — pollers stay process-level; they read `task.organization_id` and propagate it via events/notifications

- [ ] **Step 1: Update analyzers**

In `agent/po_analyzer.py`:
- Line 57: keep `select(FreeformConfig).where(FreeformConfig.enabled == True)` — the cron loop intentionally iterates all configs. Each iteration **must** then read `config.organization_id` from the loaded row.
- Line 107-108: `select(Suggestion.title).where(Suggestion.repo_id == config.repo_id)` — this is fine because `config.repo_id` already pins the org. But the new Suggestion row created later must include `organization_id=config.organization_id`.

Find the Suggestion constructor call in `po_analyzer.py` (likely at the end of the loop body where it inserts the suggestion). Stamp `organization_id=config.organization_id` on it.

Same pattern for `agent/architect_analyzer.py`.

- [ ] **Step 2: Update `orchestrator/queue.py`**

If `queue.py` has helpers like `next_eligible_task()` or `can_start_task()` that query `Task`, leave them un-scoped for the dispatcher (which runs process-level), but verify there's no user-facing endpoint that calls them — if there is, that endpoint needs to add the scoping.

```bash
grep -rn "queue\.\(next_eligible_task\|can_start_task\)" --include="*.py" .
```

For each callsite, confirm it's either (a) a process-level worker (fine as-is) or (b) a user-facing endpoint that needs scoping.

- [ ] **Step 3: Update `orchestrator/metrics.py`**

If metrics endpoints exist in the router, they must be scoped. If they're internal-only (e.g., for Prometheus), they aggregate across all orgs — that's fine for now (per-org Prometheus tags are Phase 6 work).

- [ ] **Step 4: Update `orchestrator/repo_sync.py`**

`select(Repo).where(Repo.name == name)` — this name lookup now needs to be scoped. Callers must pass `org_id`. Find callsites:

```bash
grep -rn "repo_sync\." --include="*.py" .
```

For task-context callers, `org_id = task.organization_id`. For poller-context (no task), the function should require `org_id` as a parameter — there is no "global repo by name" anymore.

- [ ] **Step 5: Update `orchestrator/deduplicator.py`**

The deduplicator finds tasks with the same `source_id` to avoid double-creating. The dedup MUST be scoped per-org — two different orgs receiving the same Slack message ID (unlikely but possible) shouldn't dedupe each other's tasks.

```python
existing = (await session.execute(
    scoped(
        select(Task).where(Task.source == source, Task.source_id == source_id),
        Task, org_id=org_id,
    )
)).scalar_one_or_none()
```

- [ ] **Step 6: Update `orchestrator/feedback.py`**

All `TaskOutcome` and `TaskHistory` queries get `scoped(...)` (both are transitively-scoped via Task).

- [ ] **Step 7: Update pollers in `run.py`**

The pollers (`ci_status_poller`, `pr_merge_poller`, `pr_comment_poller`) iterate **all** AWAITING_* tasks across all orgs by design. Don't scope them. But where they propagate downstream (call notifiers, dispatch agents), they must pass `org_id=task.organization_id`.

Find the call sites in each poller that touch downstream code (notifier, agent dispatch) and thread `task.organization_id` through. Most of those downstream functions (`shared/github_auth.get_github_token`) already accept `org_id`.

- [ ] **Step 8: Run full suite**

```bash
.venv/bin/python3 -m pytest tests/ -q
```
Expected: 0 failures.

- [ ] **Step 9: Commit**

```bash
git add agent/po_analyzer.py agent/architect_analyzer.py orchestrator/queue.py orchestrator/metrics.py orchestrator/deduplicator.py orchestrator/repo_sync.py orchestrator/feedback.py run.py
git commit -m "feat(scoping): propagate organization_id through agent + orchestrator internals"
```

---

# Task 13: WebSocket org-scoped broadcast

**Files:**
- Modify: `web/main.py:140-180` (`_broadcast` signature), `:154-287` (`websocket_endpoint`), `:877-983` (event listeners)

- [ ] **Step 1: Write a failing test**

```python
# tests/test_ws_org_scope.py
"""WS broadcasts only reach clients in the same org as the event."""

import pytest
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_broadcast_skips_clients_in_other_orgs(monkeypatch):
    """The broadcast loop must filter clients by current_org_id."""
    from web.main import _broadcast, _connected_clients
    client_a = AsyncMock()
    client_a.current_org_id = 1
    client_b = AsyncMock()
    client_b.current_org_id = 2
    _connected_clients.update({"a": client_a, "b": client_b})
    try:
        await _broadcast({"type": "task.created", "task_id": 99}, org_id=1)
        client_a.send_json.assert_awaited_once()
        client_b.send_json.assert_not_awaited()
    finally:
        _connected_clients.clear()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_ws_org_scope.py -v
```
Expected: FAIL (no `org_id` param on `_broadcast`).

- [ ] **Step 3: Update `_broadcast`**

```python
async def _broadcast(message: dict, *, org_id: int | None = None) -> None:
    """Broadcast ``message`` to all connected clients.

    If ``org_id`` is provided, only clients whose ``current_org_id`` matches
    receive the message. If ``org_id`` is None, the message goes to all
    clients (kept for system-level broadcasts that don't have a tenant —
    rare, audit each callsite before passing None).
    """
    for client_id, client in list(_connected_clients.items()):
        if org_id is not None and getattr(client, "current_org_id", None) != org_id:
            continue
        try:
            await client.send_json(message)
        except Exception:
            log.warning("ws_send_failed", client_id=client_id)
```

- [ ] **Step 4: Stamp `current_org_id` on each WS client on connect**

In `websocket_endpoint` (line 154-287), after auth resolution:

```python
payload = verify_token(token)
if not payload or "current_org_id" not in payload:
    await websocket.close(code=4401)
    return
client.user_id = payload["user_id"]
client.current_org_id = payload["current_org_id"]
```

- [ ] **Step 5: Pass `org_id` everywhere `_broadcast` is called**

For each callsite, look up the source row's `organization_id`:

```bash
grep -n "_broadcast(" web/main.py
```

For task-related broadcasts: load the task (or have it in scope), pass `org_id=task.organization_id`. For agent stream events: the Redis channel format is `task:{task_id}:stream` — load the task to get its org.

For the on-connect initial task list dump (line 179), already-scoped: change to `scoped(select(Task), Task, org_id=client.current_org_id)`.

- [ ] **Step 6: Run test + suite**

```bash
.venv/bin/python3 -m pytest tests/test_ws_org_scope.py tests/ -q
```
Expected: 0 failures.

- [ ] **Step 7: Commit**

```bash
git add web/main.py tests/test_ws_org_scope.py
git commit -m "feat(ws): broadcasts filter by current_org_id at delivery time"
```

---

# Task 14: Member management endpoints

**Files:**
- Create: `orchestrator/orgs.py`
- Modify: `orchestrator/router.py` — import the new router and `app.include_router(orgs.router)`
- Test: `tests/test_org_membership.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_org_membership.py
"""Member management — invite by email, list, remove, role change."""

import pytest


@pytest.mark.asyncio
async def test_list_my_orgs_returns_user_memberships(api_client, two_orgs):
    headers = {"Cookie": f"auto_agent_session={two_orgs.token_a}"}
    resp = await api_client.get("/api/orgs/me", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["orgs"]) == 1
    assert body["orgs"][0]["id"] == two_orgs.org_a.id
    assert body["orgs"][0]["role"] == "owner"
    assert body["current"]["id"] == two_orgs.org_a.id


@pytest.mark.asyncio
async def test_switch_current_org_reissues_token(api_client, two_orgs, monkeypatch):
    # Add user_a to org_b as a member so they have 2 orgs.
    from shared.database import async_session
    from shared.models import OrganizationMembership
    async with async_session() as s:
        s.add(OrganizationMembership(
            org_id=two_orgs.org_b.id, user_id=two_orgs.user_a.id, role="member",
        ))
        await s.commit()

    headers = {"Cookie": f"auto_agent_session={two_orgs.token_a}"}
    resp = await api_client.post(
        "/api/me/current-org",
        json={"org_id": two_orgs.org_b.id}, headers=headers,
    )
    assert resp.status_code == 200
    new_cookie = resp.cookies.get("auto_agent_session")
    assert new_cookie

    import jwt
    from orchestrator.auth import JWT_SECRET
    payload = jwt.decode(new_cookie, JWT_SECRET, algorithms=["HS256"])
    assert payload["current_org_id"] == two_orgs.org_b.id


@pytest.mark.asyncio
async def test_switch_to_org_user_not_member_of_403(api_client, two_orgs):
    headers = {"Cookie": f"auto_agent_session={two_orgs.token_a}"}
    resp = await api_client.post(
        "/api/me/current-org",
        json={"org_id": two_orgs.org_b.id}, headers=headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_invite_existing_user_to_org(api_client, two_orgs):
    # user_a invites user_b to org_a.
    headers = {"Cookie": f"auto_agent_session={two_orgs.token_a}"}
    # user_b needs an email for invite-by-email; set one.
    from shared.database import async_session
    async with async_session() as s:
        two_orgs.user_b.email = "bob@example.com"
        s.add(two_orgs.user_b)
        await s.commit()

    resp = await api_client.post(
        f"/api/orgs/{two_orgs.org_a.id}/members",
        json={"email": "bob@example.com", "role": "member"},
        headers=headers,
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_invite_unknown_email_404(api_client, two_orgs):
    headers = {"Cookie": f"auto_agent_session={two_orgs.token_a}"}
    resp = await api_client.post(
        f"/api/orgs/{two_orgs.org_a.id}/members",
        json={"email": "ghost@example.com", "role": "member"},
        headers=headers,
    )
    assert resp.status_code == 404
    assert "no_such_user" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_non_admin_cannot_invite(api_client, two_orgs):
    # Make user_b a plain member of org_a.
    from shared.database import async_session
    from shared.models import OrganizationMembership
    async with async_session() as s:
        s.add(OrganizationMembership(
            org_id=two_orgs.org_a.id, user_id=two_orgs.user_b.id, role="member",
        ))
        await s.commit()
    # Mint a token for user_b active in org_a.
    from orchestrator.auth import create_token
    token = create_token(two_orgs.user_b.id, two_orgs.user_b.username,
                         current_org_id=two_orgs.org_a.id)
    headers = {"Cookie": f"auto_agent_session={token}"}
    resp = await api_client.post(
        f"/api/orgs/{two_orgs.org_a.id}/members",
        json={"email": "anyone@example.com", "role": "member"},
        headers=headers,
    )
    assert resp.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_org_membership.py -v
```
Expected: 6 failures (endpoints don't exist).

- [ ] **Step 3: Write the orgs router**

```python
# orchestrator/orgs.py
"""Member management — list, invite, remove, role-change, switch active org."""

from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.auth import (
    COOKIE_NAME, create_token, current_org_id, current_user_id,
)
from shared.database import get_session
from shared.models import Organization, OrganizationMembership, User

router = APIRouter()


class OrgOut(BaseModel):
    id: int
    name: str
    slug: str
    role: str


class MyOrgsResponse(BaseModel):
    orgs: list[OrgOut]
    current: OrgOut


class SwitchOrgRequest(BaseModel):
    org_id: int


class InviteRequest(BaseModel):
    email: str
    role: str = "member"


class RoleChangeRequest(BaseModel):
    role: str


@router.get("/api/orgs/me", response_model=MyOrgsResponse)
async def list_my_orgs(
    user_id: int = Depends(current_user_id),
    org_id: int = Depends(current_org_id),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(
        select(Organization, OrganizationMembership.role)
        .join(OrganizationMembership, OrganizationMembership.org_id == Organization.id)
        .where(OrganizationMembership.user_id == user_id)
        .order_by(Organization.created_at.asc())
    )).all()
    orgs = [OrgOut(id=o.id, name=o.name, slug=o.slug, role=role) for o, role in rows]
    current = next((o for o in orgs if o.id == org_id), orgs[0] if orgs else None)
    if current is None:
        raise HTTPException(403, "User has no memberships")
    return MyOrgsResponse(orgs=orgs, current=current)


@router.post("/api/me/current-org")
async def switch_current_org(
    payload: SwitchOrgRequest,
    response: Response,
    user_id: int = Depends(current_user_id),
    session: AsyncSession = Depends(get_session),
):
    membership = (await session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.org_id == payload.org_id,
        )
    )).scalar_one_or_none()
    if membership is None:
        raise HTTPException(403, "Not a member of that organization")

    user = (await session.execute(
        select(User).where(User.id == user_id)
    )).scalar_one()
    new_token = create_token(user.id, user.username, current_org_id=payload.org_id)
    response.set_cookie(COOKIE_NAME, new_token, httponly=True, samesite="lax")
    from datetime import datetime, UTC
    membership.last_active_at = datetime.now(UTC)
    await session.commit()
    return {"current_org_id": payload.org_id}


def _require_admin(role: str | None) -> None:
    if role not in ("owner", "admin"):
        raise HTTPException(403, "Admin or owner role required")


@router.get("/api/orgs/{target_org_id}/members")
async def list_members(
    target_org_id: int,
    user_id: int = Depends(current_user_id),
    org_id: int = Depends(current_org_id),
    session: AsyncSession = Depends(get_session),
):
    if target_org_id != org_id:
        raise HTTPException(404, "Org not found")
    me = (await session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.org_id == org_id,
            OrganizationMembership.user_id == user_id,
        )
    )).scalar_one_or_none()
    if me is None:
        raise HTTPException(404, "Org not found")

    rows = (await session.execute(
        select(User, OrganizationMembership.role, OrganizationMembership.created_at)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .where(OrganizationMembership.org_id == org_id)
        .order_by(OrganizationMembership.created_at.asc())
    )).all()
    return {"members": [
        {"id": u.id, "username": u.username, "display_name": u.display_name,
         "email": u.email, "role": role, "joined_at": joined.isoformat()}
        for u, role, joined in rows
    ]}


@router.post("/api/orgs/{target_org_id}/members", status_code=201)
async def invite_member(
    target_org_id: int,
    payload: InviteRequest,
    user_id: int = Depends(current_user_id),
    org_id: int = Depends(current_org_id),
    session: AsyncSession = Depends(get_session),
):
    if target_org_id != org_id:
        raise HTTPException(404, "Org not found")

    me = (await session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.org_id == org_id,
            OrganizationMembership.user_id == user_id,
        )
    )).scalar_one_or_none()
    if me is None:
        raise HTTPException(404, "Org not found")
    _require_admin(me.role)

    if payload.role not in ("admin", "member"):
        raise HTTPException(400, "Role must be 'admin' or 'member'")

    target = (await session.execute(
        select(User).where(User.email == payload.email)
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "no_such_user — ask them to sign up first")

    existing = (await session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.org_id == org_id,
            OrganizationMembership.user_id == target.id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "Already a member")

    session.add(OrganizationMembership(
        org_id=org_id, user_id=target.id, role=payload.role,
    ))
    await session.commit()
    return {"user_id": target.id, "role": payload.role}


@router.delete("/api/orgs/{target_org_id}/members/{target_user_id}")
async def remove_member(
    target_org_id: int,
    target_user_id: int,
    user_id: int = Depends(current_user_id),
    org_id: int = Depends(current_org_id),
    session: AsyncSession = Depends(get_session),
):
    if target_org_id != org_id:
        raise HTTPException(404, "Org not found")
    me = (await session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.org_id == org_id,
            OrganizationMembership.user_id == user_id,
        )
    )).scalar_one_or_none()
    if me is None:
        raise HTTPException(404, "Org not found")
    _require_admin(me.role)

    target = (await session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.org_id == org_id,
            OrganizationMembership.user_id == target_user_id,
        )
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "User is not a member of this org")
    if target.role == "owner":
        raise HTTPException(400, "Cannot remove the owner — transfer ownership first")

    await session.delete(target)
    await session.commit()
    return {"removed": True}


@router.patch("/api/orgs/{target_org_id}/members/{target_user_id}")
async def change_role(
    target_org_id: int,
    target_user_id: int,
    payload: RoleChangeRequest,
    user_id: int = Depends(current_user_id),
    org_id: int = Depends(current_org_id),
    session: AsyncSession = Depends(get_session),
):
    if target_org_id != org_id:
        raise HTTPException(404, "Org not found")
    me = (await session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.org_id == org_id,
            OrganizationMembership.user_id == user_id,
        )
    )).scalar_one_or_none()
    if me is None or me.role != "owner":
        raise HTTPException(403, "Only the owner can change roles")
    if payload.role not in ("admin", "member"):
        raise HTTPException(400, "Role must be 'admin' or 'member'")

    target = (await session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.org_id == org_id,
            OrganizationMembership.user_id == target_user_id,
        )
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "User is not a member of this org")
    if target.role == "owner":
        raise HTTPException(400, "Cannot demote the owner")
    target.role = payload.role
    await session.commit()
    return {"user_id": target_user_id, "role": payload.role}
```

- [ ] **Step 4: Wire the router**

In `orchestrator/router.py` (or wherever `app.include_router` lives — usually `run.py`):

```python
from orchestrator import orgs
app.include_router(orgs.router)
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python3 -m pytest tests/test_org_membership.py -v
```
Expected: 6 passes.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/orgs.py orchestrator/router.py run.py tests/test_org_membership.py
git commit -m "feat(orgs): member CRUD + active-org switcher"
```

---

# Task 15: Org switcher UI + organization settings page

**Files:**
- Create: `web-next/lib/orgs.ts`
- Create: `web-next/hooks/use-current-org.ts`
- Create: `web-next/components/org-switcher.tsx`
- Create: `web-next/app/(app)/settings/organization/page.tsx`
- Modify: `web-next/app/(app)/layout.tsx`
- Modify: `web-next/app/(app)/settings/layout.tsx` — add a sidebar entry for "Organization"

- [ ] **Step 1: API client helpers**

```ts
// web-next/lib/orgs.ts
export type OrgMember = {
  id: number;
  username: string;
  display_name: string;
  email: string | null;
  role: "owner" | "admin" | "member";
  joined_at: string;
};

export type Org = { id: number; name: string; slug: string; role: OrgMember["role"] };

export async function fetchMyOrgs(): Promise<{ orgs: Org[]; current: Org }> {
  const r = await fetch("/api/orgs/me", { credentials: "include" });
  if (!r.ok) throw new Error("fetchMyOrgs failed");
  return r.json();
}

export async function switchOrg(org_id: number): Promise<void> {
  const r = await fetch("/api/me/current-org", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ org_id }),
  });
  if (!r.ok) throw new Error("switchOrg failed");
}

export async function fetchMembers(org_id: number): Promise<{ members: OrgMember[] }> {
  const r = await fetch(`/api/orgs/${org_id}/members`, { credentials: "include" });
  if (!r.ok) throw new Error("fetchMembers failed");
  return r.json();
}

export async function inviteMember(org_id: number, email: string, role: "admin" | "member") {
  const r = await fetch(`/api/orgs/${org_id}/members`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, role }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail ?? "invite failed");
  }
  return r.json();
}

export async function removeMember(org_id: number, user_id: number) {
  const r = await fetch(`/api/orgs/${org_id}/members/${user_id}`, {
    method: "DELETE",
    credentials: "include",
  });
  if (!r.ok) throw new Error("removeMember failed");
}
```

- [ ] **Step 2: TanStack Query hook**

```ts
// web-next/hooks/use-current-org.ts
import { useQuery } from "@tanstack/react-query";
import { fetchMyOrgs } from "@/lib/orgs";

export function useMyOrgs() {
  return useQuery({ queryKey: ["orgs", "me"], queryFn: fetchMyOrgs });
}
```

- [ ] **Step 3: Org switcher dropdown**

```tsx
// web-next/components/org-switcher.tsx
"use client";
import { useMyOrgs } from "@/hooks/use-current-org";
import { switchOrg } from "@/lib/orgs";
import { useRouter } from "next/navigation";

export function OrgSwitcher() {
  const { data, isLoading } = useMyOrgs();
  const router = useRouter();
  if (isLoading || !data) return null;
  if (data.orgs.length <= 1) {
    return <span className="text-sm text-muted-foreground">{data.current.name}</span>;
  }
  return (
    <select
      className="text-sm bg-transparent border rounded px-2 py-1"
      value={data.current.id}
      onChange={async (e) => {
        await switchOrg(Number(e.target.value));
        router.refresh();
      }}
    >
      {data.orgs.map((o) => (
        <option key={o.id} value={o.id}>{o.name}</option>
      ))}
    </select>
  );
}
```

- [ ] **Step 4: Render switcher in app layout**

In `web-next/app/(app)/layout.tsx`, add `<OrgSwitcher />` in the header bar (find the header JSX and slot it in next to the user menu).

- [ ] **Step 5: Organization settings page**

```tsx
// web-next/app/(app)/settings/organization/page.tsx
"use client";
import { useMyOrgs } from "@/hooks/use-current-org";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchMembers, inviteMember, removeMember } from "@/lib/orgs";
import { useState } from "react";

export default function OrganizationSettingsPage() {
  const { data: orgs } = useMyOrgs();
  const current = orgs?.current;
  const qc = useQueryClient();

  const { data: members } = useQuery({
    queryKey: ["members", current?.id],
    queryFn: () => current ? fetchMembers(current.id) : Promise.resolve({ members: [] }),
    enabled: !!current,
  });

  const invite = useMutation({
    mutationFn: ({ email, role }: { email: string; role: "admin" | "member" }) =>
      inviteMember(current!.id, email, role),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["members", current?.id] }),
  });

  const remove = useMutation({
    mutationFn: (user_id: number) => removeMember(current!.id, user_id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["members", current?.id] }),
  });

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"admin" | "member">("member");

  if (!current) return <div>Loading…</div>;
  const canManage = current.role === "owner" || current.role === "admin";

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold">{current.name}</h1>
        <p className="text-sm text-muted-foreground">slug: {current.slug}</p>
      </header>

      <section>
        <h2 className="text-lg font-medium mb-2">Members</h2>
        <table className="w-full text-sm">
          <thead className="text-left text-muted-foreground">
            <tr><th className="py-2">Name</th><th>Email</th><th>Role</th><th></th></tr>
          </thead>
          <tbody>
            {members?.members.map((m) => (
              <tr key={m.id} className="border-t">
                <td className="py-2">{m.display_name}</td>
                <td>{m.email ?? "—"}</td>
                <td>{m.role}</td>
                <td>
                  {canManage && m.role !== "owner" && (
                    <button
                      className="text-red-600 hover:underline"
                      onClick={() => remove.mutate(m.id)}
                    >Remove</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {canManage && (
        <section className="border-t pt-4">
          <h2 className="text-lg font-medium mb-2">Invite member</h2>
          <form
            className="flex gap-2 items-end"
            onSubmit={(e) => { e.preventDefault(); invite.mutate({ email, role }); setEmail(""); }}
          >
            <label className="flex flex-col text-sm">
              Email
              <input
                className="border rounded px-2 py-1"
                value={email} onChange={(e) => setEmail(e.target.value)} required
              />
            </label>
            <label className="flex flex-col text-sm">
              Role
              <select
                className="border rounded px-2 py-1"
                value={role} onChange={(e) => setRole(e.target.value as "admin" | "member")}
              >
                <option value="member">Member</option>
                <option value="admin">Admin</option>
              </select>
            </label>
            <button className="bg-black text-white px-3 py-1 rounded" type="submit"
                    disabled={invite.isPending}>
              {invite.isPending ? "Inviting…" : "Invite"}
            </button>
          </form>
          {invite.error && (
            <p className="text-red-600 text-sm mt-2">{String(invite.error)}</p>
          )}
        </section>
      )}
    </div>
  );
}
```

- [ ] **Step 6: Add "Organization" to settings sidebar**

Modify `web-next/app/(app)/settings/layout.tsx` — add a sidebar link to `/settings/organization`.

- [ ] **Step 7: Manual verification (TypeScript + UI)**

```bash
cd web-next && npm run build
```
Expected: clean type-check + build.

Start the dev server, click "Settings → Organization", verify:
- Members table renders.
- Invite form works (against a known-existing user).
- Remove button hides for the owner row.

- [ ] **Step 8: Commit**

```bash
git add web-next/
git commit -m "feat(ui): org switcher + organization settings page"
```

---

# Task 16: Repo namespace — `unique(organization_id, name)` instead of `unique(name)`

**Files:**
- Modify: `migrations/versions/026_organizations.py` — append the constraint swap to `upgrade()`
- Modify: `shared/models.py` — drop `unique=True` from `Repo.name`, add a composite `UniqueConstraint`
- Test: `tests/test_repo_namespace.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_repo_namespace.py
"""Repos can share a name across orgs but not within an org."""

import pytest
from shared.database import async_session
from shared.models import Repo


@pytest.mark.asyncio
async def test_same_repo_name_in_two_orgs(two_orgs):
    async with async_session() as s:
        r1 = Repo(name="shared-name", url="x", organization_id=two_orgs.org_a.id)
        r2 = Repo(name="shared-name", url="y", organization_id=two_orgs.org_b.id)
        s.add_all([r1, r2])
        await s.commit()
        await s.delete(r1); await s.delete(r2)
        await s.commit()


@pytest.mark.asyncio
async def test_duplicate_name_in_same_org_rejected(two_orgs):
    async with async_session() as s:
        r1 = Repo(name="conflict", url="x", organization_id=two_orgs.org_a.id)
        s.add(r1)
        await s.commit()
        r2 = Repo(name="conflict", url="y", organization_id=two_orgs.org_a.id)
        s.add(r2)
        with pytest.raises(Exception):
            await s.commit()
        await s.rollback()
        await s.delete(r1)
        await s.commit()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_repo_namespace.py -v
```
Expected: first test fails (duplicate global name forbidden by current `unique=True`).

- [ ] **Step 3: Amend migration 026 + update ORM**

Append to `026_organizations.py::upgrade()`:

```python
op.execute("ALTER TABLE repos DROP CONSTRAINT IF EXISTS repos_name_key")
op.execute("DROP INDEX IF EXISTS ix_repos_name")
op.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_repos_org_name "
    "ON repos (organization_id, name)"
)
```

And in `downgrade()`:

```python
op.execute("DROP INDEX IF EXISTS ix_repos_org_name")
op.execute("ALTER TABLE repos ADD CONSTRAINT repos_name_key UNIQUE (name)")
```

In `shared/models.py::Repo`, change:

```python
name = Column(String(255), nullable=False, unique=True)
```

to:

```python
name = Column(String(255), nullable=False)  # uniqueness enforced by ix_repos_org_name
```

And below `created_at = ...`:

```python
__table_args__ = (
    UniqueConstraint("organization_id", "name", name="ix_repos_org_name"),
)
```

Add `UniqueConstraint` to the SQLAlchemy import at the top of the file.

- [ ] **Step 4: Re-run migration + tests**

```bash
.venv/bin/python3 -m alembic downgrade 025
.venv/bin/python3 -m alembic upgrade 026
.venv/bin/python3 -m pytest tests/test_repo_namespace.py -v
```
Expected: 2 passes.

- [ ] **Step 5: Run full suite + isolation test**

```bash
.venv/bin/python3 -m pytest tests/ -q
```
Expected: 0 failures.

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/026_organizations.py shared/models.py tests/test_repo_namespace.py
git commit -m "feat(repos): unique(organization_id, name) instead of global unique"
```

---

# Task 17: Migration 027 — flip NOT NULL + indexes

**Files:**
- Create: `migrations/versions/027_organizations_not_null.py`

> Run this **last**, after every endpoint has been scoped and the parametrized isolation suite is fully green. The NOT NULL flip is the safety net that prevents future bugs from inserting rows without an `organization_id`.

- [ ] **Step 1: Write the migration**

```python
# migrations/versions/027_organizations_not_null.py
"""Phase 2 — Flip organization_id to NOT NULL + add indexes.

Run after migration 026 backfill is verified. The NOT NULL constraint
prevents future inserts without an organization_id — the load-bearing
guarantee for tenant isolation at the schema level.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "027"
down_revision: Union[str, None] = "026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SCOPED_TABLES = [
    "users", "repos", "tasks", "scheduled_tasks",
    "suggestions", "freeform_configs", "search_sessions",
    "user_secrets",
]


def upgrade() -> None:
    for table in SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN organization_id SET NOT NULL")

    op.execute("CREATE INDEX IF NOT EXISTS ix_tasks_org_status ON tasks(organization_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_repos_org ON repos(organization_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_suggestions_org_status ON suggestions(organization_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_freeform_org ON freeform_configs(organization_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_scheduled_org ON scheduled_tasks(organization_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_search_org ON search_sessions(organization_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_search_org")
    op.execute("DROP INDEX IF EXISTS ix_scheduled_org")
    op.execute("DROP INDEX IF EXISTS ix_freeform_org")
    op.execute("DROP INDEX IF EXISTS ix_suggestions_org_status")
    op.execute("DROP INDEX IF EXISTS ix_repos_org")
    op.execute("DROP INDEX IF EXISTS ix_tasks_org_status")
    for table in reversed(SCOPED_TABLES):
        op.execute(f"ALTER TABLE {table} ALTER COLUMN organization_id DROP NOT NULL")
```

- [ ] **Step 2: Run migration**

```bash
.venv/bin/python3 -m alembic upgrade head
```
Expected: `Running upgrade 026 -> 027`.

- [ ] **Step 3: Update ORM models to reflect NOT NULL**

For each scoped model in `shared/models.py`, change:
```python
organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
```
to:
```python
organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
```

- [ ] **Step 4: Run full suite**

```bash
.venv/bin/python3 -m pytest tests/ -q
```
Expected: 0 failures. If anything fails, it means an insert path was missed in earlier tasks — go back and fix.

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/027_organizations_not_null.py shared/models.py
git commit -m "feat(migration): 027 — organization_id NOT NULL + indexes"
```

---

# Task 18: Final verification + PR

- [ ] **Step 1: Run full test suite**

```bash
.venv/bin/python3 -m pytest tests/ -q
```
Expected: all tests pass (~770 with new tests added).

- [ ] **Step 2: Run linter and formatter**

```bash
ruff check .
ruff format --check .
```
Expected: clean.

- [ ] **Step 3: Run isolation suite explicitly**

```bash
.venv/bin/python3 -m pytest tests/test_org_isolation.py -v
```
Expected: every parametrized case green.

- [ ] **Step 4: Self-review the diff**

```bash
git diff main...HEAD --stat
git diff main...HEAD | less
```

Look specifically for:
- Any `select(Task | Repo | Suggestion | FreeformConfig | ScheduledTask | SearchSession)` in `orchestrator/router.py` not wrapped in `scoped(...)`.
- Any new endpoint added by the user that isn't in `test_org_isolation.py`.
- Any `create_token(...)` callsite without `current_org_id=`.
- Any `_broadcast(...)` callsite without `org_id=`.

If found, fix and add an extra commit.

- [ ] **Step 5: Run TypeScript build**

```bash
cd web-next && npm run build
```
Expected: clean build.

- [ ] **Step 6: Manual smoke (against staging or local)**

```bash
docker compose up -d
# Then in browser:
# 1. Sign up a new user "alice@test.com"
# 2. Verify email (or grab the link from logs)
# 3. Confirm /settings/organization shows "Alice" as the org name
# 4. Sign up "bob@test.com" → personal org
# 5. As Alice, invite bob@test.com — confirm Bob shows in members
# 6. As Bob (after the invite), use org switcher to flip to Alice's org
# 7. Verify Bob sees Alice's repos/tasks; Alice doesn't see Bob's personal-org repos
```

- [ ] **Step 7: Push branch + open PR**

```bash
git push -u origin feat/multi-tenant-org-model
gh pr create --title "feat(multi-tenant): Phase 2 — org/tenant model + cross-org isolation" --body "$(cat <<'EOF'
## Summary
- Adds `organizations` and `organization_memberships` tables; every customer-data row gains `organization_id`
- `scoped()` query helper + `current_org_id()` FastAPI dependency
- Every endpoint in `orchestrator/router.py` (+ analyzers, queue, dedup, feedback, search, repo_sync) filters by active org
- Parametrized cross-org isolation test (`tests/test_org_isolation.py`) — every tenant endpoint represented
- Org switcher UI + organization settings page (member CRUD)
- Migration 026 (nullable + backfill) + 027 (NOT NULL + indexes)
- M2M user↔org via `organization_memberships(role)` — `owner` / `admin` / `member`

## Test plan
- [ ] `pytest tests/ -q` — all green
- [ ] `pytest tests/test_org_isolation.py -v` — every parametrize case passes
- [ ] `ruff check .` — clean
- [ ] `npm run build` (web-next) — clean
- [ ] Manual smoke: signup → personal org → invite → switch → verify cross-org invisibility

## Out of scope (deferred)
- Cross-org invitations for non-existent users (Phase 6 — needs invite-email click-through)
- Per-org concurrency caps (Phase 4)
- Per-org Slack/GitHub OAuth (Phase 3)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 8: Commit task list completion + update handover doc**

After PR is open and CI passes, write a follow-up handover doc at `docs/superpowers/plans/YYYY-MM-DD-handover-after-phase-2.md` summarizing what's done and what's next (Phase 3: per-org Slack + GitHub OAuth).

---

# Notes for the executing engineer

**Migrations are sticky.** Once 026 is applied to prod, you cannot blow it away. If you change the shape of 026 mid-flight, drop the prod DB to migration 025 first via `alembic downgrade 025` (only safe pre-launch; never run on a DB with real customer data).

**The two-Postgres trap.** The user's local Mac has both a Mac-native Postgres and a Docker Postgres on the same port. `.env`'s DATABASE_URL points at Mac-native; docker-compose overrides to Docker. Use Mac-native for fast iteration; deploy to the VM for the real test. Don't burn time on local Docker — the user has explicitly said they don't test there.

**The macOS Claude-auth quirk.** `claude auth status` will fail inside the container on macOS dev. `shared/preflight.py` already downgrades this to a warning. Don't try to "fix" it — it's expected.

**`shared/secrets.py` PK change is the riskiest sub-step.** If you mis-write the `ALTER TABLE` for the user_secrets PK, you can lock yourself out of decrypting existing rows. Always run the migration against a backup-restorable DB first. If you're nervous, write a one-off Python script that decrypts all rows with the old key, drops them, and re-inserts with the new key — slower but reversible.

**The `_broadcast` filtering must be defensive.** A bug there leaks live updates across orgs. Add a log line every time `_broadcast` skips a client because of an org mismatch — easy to assert in tests, easy to spot in prod logs.

**Don't refactor the SPA bridge.** `web/main.py` is half-decommissioned (per CLAUDE.md). The WS handlers there are still production code. Modify only what's needed for org scoping; don't try to migrate WS to Next.js in this PR.

**One bundled PR per the user's choice.** When you push, don't split into multiple PRs. Each commit inside the PR should be independently readable, but they merge as one.
