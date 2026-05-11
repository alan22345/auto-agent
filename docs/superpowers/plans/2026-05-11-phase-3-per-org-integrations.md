# Phase 3 — Per-org Slack + GitHub OAuth Integrations

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-app Slack bot + single-installation GitHub App with per-org OAuth installations, so a new tenant can self-serve "Install on Slack" and "Install GitHub App" from `/settings/integrations/...` without env-var changes.

**Architecture:** One Slack app (distributed) installed in many workspaces — `slack-bolt`'s multi-team mode keyed on `team_id` → `org_id`. One GitHub App installed per customer org, `installation_id` minted on demand. Per-tenant bot tokens, app installation IDs, and webhook secrets all stored encrypted in Postgres (pgcrypto). Legacy env-var paths remain as fallback for the dev-VM single-tenant install.

**Tech Stack:** Python 3.12, `slack-bolt[async]` (already vendored), `httpx`, PyJWT, FastAPI, SQLAlchemy async, Alembic, pgcrypto. UI: Next.js (App Router) + TanStack Query.

---

## Pre-flight (read before starting)

- **Branch off `main`** at `9a5c2c1` (Phase 2 merge). Suggested branch: `feat/phase-3-per-org-integrations`.
- **Phase 2 is live** in the codebase but **NOT yet deployed to prod**. The handover (`docs/superpowers/plans/2026-05-11-handover-after-phase-2.md`) lists three pre-Phase-3 items the user accepted skipping: real signup verification, Phase 2 deploy smoke test, and the testcontainers-backed real-DB isolation suite. **None of these block Phase 3 code-wise** — they're operational. Flag them again in the Phase 3 handover.
- **Slack app must be re-registered as "distributed"** in `api.slack.com/apps` before this code can be tested end-to-end. The current Slack app is a single-workspace install. Registering distribution is reversible. Whoever runs the deploy needs OAuth client_id + client_secret in `.env`.
- **GitHub App must have its webhook URL set** to `https://<prod-domain>/api/webhooks/github` and `installation` events enabled before Phase 3 can react to uninstalls. The app may already have this — verify before merging.

---

## Glossary

| Term | Meaning |
|---|---|
| **Installation** | One row in `slack_installations` or `github_installations` representing a customer's auth grant to our app. |
| **`team_id`** | Slack workspace ID (`T...`). The primary key Slack tags every event with. |
| **`installation_id`** (GitHub) | The numeric ID GitHub assigns when an org installs our App. Used as the URL parameter when minting an installation token. |
| **Bot token** | Slack's `xoxb-…` token, per workspace, granted on install. Used for `chat.postMessage`, `conversations.open`, etc. |
| **App token** | Slack's `xapp-…` token, per *app* (not per workspace). One value for the whole distributed app. Used for Socket Mode. |
| **`AsyncInstallationStore`** | The slack-bolt abstraction that returns the bot token for a given `team_id` when an event arrives. We implement it with a Postgres lookup. |
| **`SECRETS_PASSPHRASE`** | The single pgcrypto symmetric passphrase that encrypts `user_secrets.value_enc`, `slack_installations.bot_token_enc`, `github_installations.app_private_key_enc` (if we store one), and `webhook_secrets.secret_enc`. Already set on prod from Phase 1. **Must NOT rotate during the 028 migration.** |
| **Distributed app** | Slack's term for an app that supports OAuth install into arbitrary workspaces (vs. a single-workspace app installed manually from the dashboard). |

---

## File Structure

### New files

| Path | Responsibility |
|---|---|
| `migrations/versions/028_per_org_integrations.py` | Creates `slack_installations`, `github_installations`, `webhook_secrets`. pgcrypto-encrypted BYTEA columns. |
| `integrations/slack/installation_store.py` | `PostgresInstallationStore` — implements `AsyncInstallationStore`. `async_save / async_find_bot / async_delete_installation` keyed off `team_id`. |
| `integrations/slack/oauth.py` | FastAPI router: `GET /api/integrations/slack/install`, `GET /api/integrations/slack/oauth/callback`, `POST /api/integrations/slack/uninstall`, `GET /api/integrations/slack`. Signs/verifies OAuth state. |
| `integrations/github/__init__.py` | New package marker. |
| `integrations/github/oauth.py` | FastAPI router: `GET /api/integrations/github/install`, `GET /api/integrations/github/oauth/callback`, `POST /api/integrations/github/uninstall`, `GET /api/integrations/github`. |
| `shared/installation_crypto.py` | Single owner of pgcrypto encrypt/decrypt for installation tokens. Mirrors the SQL idioms in `shared/secrets.py` so future rotation has one place to look. |
| `web-next/app/(app)/settings/integrations/page.tsx` | Hub page linking to per-integration sub-pages. |
| `web-next/app/(app)/settings/integrations/slack/page.tsx` | "Add to Slack" CTA, or workspace name + Disconnect button. |
| `web-next/app/(app)/settings/integrations/github/page.tsx` | "Install GitHub App" CTA, or account login + Disconnect. |
| `web-next/hooks/useIntegrations.ts` | TanStack Query hooks: `useSlackInstall`, `useGithubInstall`, both invalidate on uninstall. |
| `web-next/lib/integrations.ts` | Typed fetch helpers for the four endpoints above. |
| `tests/test_slack_installation_store.py` | Save/find/delete round-trips against a mocked session. |
| `tests/test_slack_oauth.py` | Install redirect → callback exchange → DB row + signed-state CSRF protection. |
| `tests/test_github_oauth.py` | Install redirect → callback (`installation_id` consumed) → DB row + signed-state CSRF. |
| `tests/test_github_auth_per_org_app.py` | `get_github_token` returns a per-org installation token when `github_installations[org_id]` exists. |
| `tests/test_slack_multi_team_routing.py` | A DM event with `team_id=Tx` routes to org X; with `team_id=Ty` routes to org Y; unknown `team_id` is dropped. |
| `tests/test_webhook_per_org_secret.py` | Github webhook payload signed with org-A's secret verifies against org A but is rejected when looked up against org B. |
| `tests/test_github_installation_deleted.py` | `installation.deleted` event removes the row and stops routing to that org. |

### Modified files

| Path | What changes |
|---|---|
| `shared/models.py` | Add `SlackInstallation`, `GitHubInstallation`, `WebhookSecret` after `UserSecret` (after line 374). |
| `shared/config.py` | Add `slack_client_id`, `slack_client_secret`, `slack_oauth_state_secret`, `github_app_slug` (e.g. "auto-agent"), `phase3_legacy_slack_token` env-var pass-through (just a clarity alias for `slack_bot_token` — same env, different docstring). |
| `shared/github_auth.py` | Insert per-org installation lookup between the user-PAT and env-app branches. Cache becomes `dict[int, _CachedToken]` keyed by `org_id`. |
| `integrations/slack/main.py` | Rewrite `_get_app()` → multi-team app using `PostgresInstallationStore`. `send_slack_dm` gains `org_id: int` kwarg. `_handle_dm_event` resolves `team_id → org_id` first. Legacy single-token fallback retained for `slack_installations` empty. |
| `orchestrator/router.py` | Mount `integrations.slack.oauth.router` and `integrations.github.oauth.router`. Add `current_org_id_admin_dep` (admin-only variant of `current_org_id_dep` — only owners/admins can install/uninstall integrations). |
| `orchestrator/webhooks/github.py` | `_verify_signature(body, sig)` → `_verify_signature(body, sig, *, org_id_hint=None)`. Resolves `org_id` from `payload.repository.full_name → Repo.organization_id`, looks up `webhook_secrets[(org_id, "github")]`, falls back to `settings.github_webhook_secret`. New event branch: `x_github_event == "installation"` → `_handle_installation_event`. |
| `orchestrator/auth.py` | New `current_org_id_admin_dep` factory that wraps `current_org_id_dep` and additionally checks `OrganizationMembership.role IN ("owner", "admin")`. |
| `run.py` | The N existing `get_github_token(user_id=..., organization_id=...)` callers keep their signatures (already correct). One change: pass `org_id` to `send_slack_dm` from notification fan-out (look up `task.organization_id`). |
| `agent/lifecycle/{coding,review,deploy}.py` | No-op for GitHub — already pass `organization_id`. Verify post-merge with a grep. |
| `web-next/components/sidebar/sidebar.tsx` | Add "Integrations" link under Settings. |
| `web-next/app/(app)/settings/layout.tsx` | Add `Integrations` to the nav list (mirrors how "Organization" was added in Phase 2). |
| `tests/test_org_scoping_coverage.py` | Add `/api/integrations/slack/oauth/callback` and `/api/integrations/github/oauth/callback` to `UNSCOPED_ALLOWLIST` with reasons (state-signed; we resolve org from the state cookie, not the JWT). |
| `docs/superpowers/plans/2026-05-11-handover-after-phase-3.md` | New handover at end. |

---

## Track A — Schema and ORM (migration 028)

Migration 028 is the load-bearing change. Pause anything else until 028 + the three new ORM models + the crypto helper are in.

### Task A1: Create migration 028 — schema only

**Files:**
- Create: `migrations/versions/028_per_org_integrations.py`
- Test: none directly — verified via Alembic upgrade/downgrade in Task A3.

- [ ] **Step 1: Write the migration**

```python
"""028 — per-org Slack + GitHub installations + webhook secrets

Adds three tables:
  * slack_installations  (1:1 with organizations, keyed by team_id)
  * github_installations (1:1 with organizations, keyed by installation_id)
  * webhook_secrets      (composite PK on (org_id, source))

Token columns are pgcrypto-encrypted BYTEA. Reuses the SECRETS_PASSPHRASE
already in use by user_secrets — the deploy MUST run with the same
passphrase across 027 → 028 or every stored token decodes to garbage.

Revision ID: 028
Revises: 027
Create Date: 2026-05-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "slack_installations",
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.String(length=32), nullable=False),
        sa.Column("team_name", sa.String(length=255), nullable=True),
        sa.Column("bot_token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("bot_user_id", sa.String(length=32), nullable=False),
        sa.Column("app_token_enc", sa.LargeBinary(), nullable=True),
        sa.Column("installed_by_slack_user_id", sa.String(length=32), nullable=True),
        sa.Column(
            "installed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("org_id"),
        sa.UniqueConstraint("team_id", name="uq_slack_installations_team_id"),
    )

    op.create_table(
        "github_installations",
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("account_login", sa.String(length=128), nullable=False),
        sa.Column("account_type", sa.String(length=32), nullable=False),  # "User" | "Organization"
        sa.Column(
            "installed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("org_id"),
        sa.UniqueConstraint(
            "installation_id", name="uq_github_installations_installation_id"
        ),
    )

    op.create_table(
        "webhook_secrets",
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),  # "github" | "linear"
        sa.Column("secret_enc", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("org_id", "source"),
    )

    op.create_index(
        "ix_slack_installations_team_id",
        "slack_installations",
        ["team_id"],
    )
    op.create_index(
        "ix_github_installations_installation_id",
        "github_installations",
        ["installation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_github_installations_installation_id", "github_installations")
    op.drop_index("ix_slack_installations_team_id", "slack_installations")
    op.drop_table("webhook_secrets")
    op.drop_table("github_installations")
    op.drop_table("slack_installations")
```

- [ ] **Step 2: Verify revision chain**

Run: `ls migrations/versions/ | tail -3`
Expected: `026_organizations.py`, `027_organizations_not_null.py`, `028_per_org_integrations.py`

Run: `grep -E "revision|down_revision" migrations/versions/028_per_org_integrations.py`
Expected: `revision = "028"` and `down_revision = "027"`.

- [ ] **Step 3: Commit**

```bash
git add migrations/versions/028_per_org_integrations.py
git commit -m "feat(migration): 028 — slack/github installations + webhook secrets"
```

---

### Task A2: ORM models for the three tables

**Files:**
- Modify: `shared/models.py` (insert after `UserSecret`, before `SearchSession`)
- Test: `tests/test_models_integrations.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models_integrations.py
"""ORM smoke test — the three new Phase 3 models import, instantiate, and
expose the load-bearing column names. Schema correctness lives in the
migration; this is type-shape coverage."""
from __future__ import annotations

from shared.models import GitHubInstallation, SlackInstallation, WebhookSecret


def test_slack_installation_columns():
    cols = {c.name for c in SlackInstallation.__table__.columns}
    assert {
        "org_id", "team_id", "team_name", "bot_token_enc",
        "bot_user_id", "app_token_enc", "installed_by_slack_user_id",
        "installed_at",
    } <= cols


def test_github_installation_columns():
    cols = {c.name for c in GitHubInstallation.__table__.columns}
    assert {
        "org_id", "installation_id", "account_login", "account_type",
        "installed_at",
    } <= cols


def test_webhook_secret_columns():
    cols = {c.name for c in WebhookSecret.__table__.columns}
    assert {"org_id", "source", "secret_enc", "created_at"} <= cols


def test_slack_installation_table_name():
    assert SlackInstallation.__tablename__ == "slack_installations"


def test_github_installation_table_name():
    assert GitHubInstallation.__tablename__ == "github_installations"


def test_webhook_secret_table_name():
    assert WebhookSecret.__tablename__ == "webhook_secrets"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_models_integrations.py -q`
Expected: FAIL — `ImportError: cannot import name 'SlackInstallation' from 'shared.models'`.

- [ ] **Step 3: Add the three models**

Append in `shared/models.py` after the `UserSecret` class definition (around line 374, before `SearchSession`):

```python
class SlackInstallation(Base):
    """A customer org's Slack workspace install — 1:1 with organizations."""

    __tablename__ = "slack_installations"

    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    team_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    team_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bot_token_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    bot_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    app_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    installed_by_slack_user_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GitHubInstallation(Base):
    """A customer org's GitHub App install — 1:1 with organizations."""

    __tablename__ = "github_installations"

    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    installation_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True
    )
    account_login: Mapped[str] = mapped_column(String(128), nullable=False)
    account_type: Mapped[str] = mapped_column(String(32), nullable=False)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WebhookSecret(Base):
    """Per-org override for inbound webhook HMAC verification."""

    __tablename__ = "webhook_secrets"

    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    secret_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

If `BigInteger` / `LargeBinary` are not already imported at the top of `shared/models.py`, add them to the existing `from sqlalchemy import ...` line.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_models_integrations.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add shared/models.py tests/test_models_integrations.py
git commit -m "feat(models): SlackInstallation, GitHubInstallation, WebhookSecret"
```

---

### Task A3: pgcrypto helper for installation tokens

**Files:**
- Create: `shared/installation_crypto.py`
- Test: `tests/test_installation_crypto.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_installation_crypto.py
"""installation_crypto wraps pgcrypto's pgp_sym_encrypt / pgp_sym_decrypt
exactly the same way shared/secrets.py does — it's a separate module only
so callers don't have to thread the model name + column shape through.

Mocks the session because Postgres isn't wired in test."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from shared import installation_crypto


@pytest.mark.asyncio
async def test_encrypt_calls_pgp_sym_encrypt(monkeypatch):
    monkeypatch.setattr(installation_crypto.settings, "secrets_passphrase", "p")
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one=lambda: b"CIPHER"))

    out = await installation_crypto.encrypt("plain-token", session=session)

    assert out == b"CIPHER"
    args, _ = session.execute.call_args
    sql_text = str(args[0])
    assert "pgp_sym_encrypt" in sql_text


@pytest.mark.asyncio
async def test_decrypt_calls_pgp_sym_decrypt(monkeypatch):
    monkeypatch.setattr(installation_crypto.settings, "secrets_passphrase", "p")
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one=lambda: "plain"))

    out = await installation_crypto.decrypt(b"CIPHER", session=session)

    assert out == "plain"
    args, _ = session.execute.call_args
    sql_text = str(args[0])
    assert "pgp_sym_decrypt" in sql_text


@pytest.mark.asyncio
async def test_encrypt_raises_without_passphrase(monkeypatch):
    monkeypatch.setattr(installation_crypto.settings, "secrets_passphrase", "")
    session = MagicMock()
    with pytest.raises(RuntimeError, match="SECRETS_PASSPHRASE"):
        await installation_crypto.encrypt("x", session=session)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_installation_crypto.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.installation_crypto'`.

- [ ] **Step 3: Write the helper**

```python
# shared/installation_crypto.py
"""pgcrypto encrypt/decrypt for installation tokens.

Separate from shared/secrets.py because secrets.py deals with
(user_id, org_id, key) tuples for the user_secrets table; installation
tokens live in their own tables with different keying. The encryption
primitive is identical and keyed off SECRETS_PASSPHRASE.

Rotation: change SECRETS_PASSPHRASE means every BYTEA blob in
user_secrets.value_enc, slack_installations.bot_token_enc,
slack_installations.app_token_enc, and webhook_secrets.secret_enc must
be re-encrypted in a coordinated upgrade. There is no rotation script
today (deferred until a customer asks)."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import settings


async def encrypt(value: str, *, session: AsyncSession) -> bytes:
    if not settings.secrets_passphrase:
        raise RuntimeError(
            "SECRETS_PASSPHRASE is not set. "
            "Set it in .env before installing integrations."
        )
    result = await session.execute(
        text("SELECT pgp_sym_encrypt(:v, :p)"),
        {"v": value, "p": settings.secrets_passphrase},
    )
    return result.scalar_one()


async def decrypt(blob: bytes, *, session: AsyncSession) -> str:
    if not settings.secrets_passphrase:
        raise RuntimeError(
            "SECRETS_PASSPHRASE is not set. "
            "Cannot decrypt installation tokens."
        )
    result = await session.execute(
        text("SELECT pgp_sym_decrypt(:b, :p)"),
        {"b": blob, "p": settings.secrets_passphrase},
    )
    return result.scalar_one()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_installation_crypto.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add shared/installation_crypto.py tests/test_installation_crypto.py
git commit -m "feat(crypto): installation_crypto helper for slack/github tokens"
```

---

### Task A4: Configuration — Slack OAuth env vars

**Files:**
- Modify: `shared/config.py`
- Test: extend `tests/test_config.py` if it exists, otherwise inline by reading the field defaults.

- [ ] **Step 1: Write the failing test**

Add to a new file or extend existing:

```python
# tests/test_config_phase3.py
from shared.config import Settings


def test_slack_oauth_fields_exist():
    s = Settings(
        # minimal mandatory fields the existing Settings requires;
        # mirror tests/test_secrets.py construction for the right shape
        anthropic_api_key="x",
        database_url="sqlite+aiosqlite:///:memory:",
        secrets_passphrase="p",
        jwt_secret="j",
        slack_client_id="cid",
        slack_client_secret="csec",
        slack_oauth_state_secret="ssec",
        github_app_slug="auto-agent",
    )
    assert s.slack_client_id == "cid"
    assert s.slack_client_secret == "csec"
    assert s.slack_oauth_state_secret == "ssec"
    assert s.github_app_slug == "auto-agent"


def test_slack_oauth_fields_default_to_none():
    s = Settings(
        anthropic_api_key="x",
        database_url="sqlite+aiosqlite:///:memory:",
        secrets_passphrase="p",
        jwt_secret="j",
    )
    assert s.slack_client_id is None
    assert s.slack_client_secret is None
    assert s.slack_oauth_state_secret is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_config_phase3.py -q`
Expected: FAIL — `slack_client_id` not on Settings.

- [ ] **Step 3: Add the fields**

In `shared/config.py`, alongside the existing Slack fields:

```python
    # --- Phase 3: per-org Slack OAuth ---
    slack_client_id: str | None = None
    slack_client_secret: str | None = None
    slack_oauth_state_secret: str | None = None
    # GitHub App slug used to build the install URL
    # (e.g. https://github.com/apps/auto-agent/installations/new)
    github_app_slug: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_config_phase3.py -q`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add shared/config.py tests/test_config_phase3.py
git commit -m "feat(config): phase-3 slack OAuth + github_app_slug settings"
```

---

## Track B — Slack OAuth installation store

### Task B1: `PostgresInstallationStore` — save round-trip

**Files:**
- Create: `integrations/slack/installation_store.py`
- Test: `tests/test_slack_installation_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_slack_installation_store.py
"""PostgresInstallationStore mirrors slack-bolt's AsyncInstallationStore
contract enough that a multi-team app can resolve bot tokens by team_id."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from integrations.slack.installation_store import PostgresInstallationStore


@pytest.mark.asyncio
async def test_save_inserts_row_with_encrypted_token():
    store = PostgresInstallationStore(org_id=42)

    # The slack-bolt Installation type has these attributes; we mock just
    # what we read.
    install = MagicMock(
        team_id="T123",
        team_name="acme",
        bot_token="xoxb-secret",
        bot_user_id="UBOTID",
        app_token=None,
        user_id="UADMIN",  # the Slack user who installed
    )

    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "integrations.slack.installation_store.async_session",
        return_value=session_cm,
    ), patch(
        "integrations.slack.installation_store.installation_crypto.encrypt",
        new=AsyncMock(return_value=b"CIPHER"),
    ):
        await store.async_save(install)

    # We expect an INSERT ... ON CONFLICT (org_id) DO UPDATE so re-install
    # is idempotent.
    call_args_list = session.execute.await_args_list
    sql_strings = [str(c.args[0]) for c in call_args_list]
    assert any("INSERT" in s and "slack_installations" in s for s in sql_strings)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_slack_installation_store.py::test_save_inserts_row_with_encrypted_token -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write `installation_store.py` (save path only — find/delete in next tasks)**

```python
# integrations/slack/installation_store.py
"""Postgres-backed slack-bolt installation store.

We don't subclass slack_bolt's AsyncInstallationStore directly to avoid a
hard dependency on its private signatures — instead we expose the same
method names slack-bolt's AsyncApp expects (`async_save`,
`async_find_bot`, `async_delete_installation`) and pass an instance into
`AsyncApp(installation_store=...)`.

Why per-org instantiation? The OAuth callback knows which org just
installed; pass that org_id into the store at construction so save() can
stamp the FK without parsing the Slack `team_id` out into a separate
mapping table.

Lookups (async_find_bot) are global — slack-bolt only gives us the
team_id, and we resolve org_id from `slack_installations.team_id`."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text

from shared import installation_crypto
from shared.database import async_session

log = logging.getLogger(__name__)


@dataclass
class BotInstallation:
    """Subset of slack-bolt's Bot data class — what AsyncApp actually
    reads from `async_find_bot`. We only fill what's needed: bot_token,
    bot_user_id, team_id."""

    bot_token: str
    bot_user_id: str
    team_id: str
    enterprise_id: str | None = None  # always None for non-Enterprise installs


class PostgresInstallationStore:
    def __init__(self, org_id: int | None = None):
        # org_id is only required at install time. async_find_bot does not
        # need it (we resolve org from team_id).
        self.org_id = org_id

    async def async_save(self, installation) -> None:  # noqa: ANN001 (slack-bolt type)
        if self.org_id is None:
            raise RuntimeError(
                "PostgresInstallationStore.async_save called without org_id "
                "— construct with org_id at OAuth callback time."
            )
        async with async_session() as session:
            bot_token_enc = await installation_crypto.encrypt(
                installation.bot_token, session=session
            )
            app_token_enc = None
            if getattr(installation, "app_token", None):
                app_token_enc = await installation_crypto.encrypt(
                    installation.app_token, session=session
                )
            await session.execute(
                text(
                    """
                    INSERT INTO slack_installations
                        (org_id, team_id, team_name, bot_token_enc,
                         bot_user_id, app_token_enc,
                         installed_by_slack_user_id, installed_at)
                    VALUES
                        (:org_id, :team_id, :team_name, :bot_token_enc,
                         :bot_user_id, :app_token_enc,
                         :installed_by, now())
                    ON CONFLICT (org_id) DO UPDATE SET
                        team_id = EXCLUDED.team_id,
                        team_name = EXCLUDED.team_name,
                        bot_token_enc = EXCLUDED.bot_token_enc,
                        bot_user_id = EXCLUDED.bot_user_id,
                        app_token_enc = EXCLUDED.app_token_enc,
                        installed_by_slack_user_id = EXCLUDED.installed_by,
                        installed_at = now()
                    """
                ),
                {
                    "org_id": self.org_id,
                    "team_id": installation.team_id,
                    "team_name": getattr(installation, "team_name", None),
                    "bot_token_enc": bot_token_enc,
                    "bot_user_id": installation.bot_user_id,
                    "app_token_enc": app_token_enc,
                    "installed_by": getattr(installation, "user_id", None),
                },
            )
            await session.commit()
        log.info(
            "slack_installation_saved org_id=%s team_id=%s",
            self.org_id,
            installation.team_id,
        )

    async def async_find_bot(
        self,
        *,
        enterprise_id: str | None = None,
        team_id: str | None = None,
        is_enterprise_install: bool | None = None,
    ) -> BotInstallation | None:
        # Implemented in Task B2.
        raise NotImplementedError

    async def async_delete_installation(
        self,
        *,
        enterprise_id: str | None = None,
        team_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        # Implemented in Task B3.
        raise NotImplementedError
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_slack_installation_store.py -q`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/slack/installation_store.py tests/test_slack_installation_store.py
git commit -m "feat(slack): PostgresInstallationStore.async_save"
```

---

### Task B2: `async_find_bot` — lookup by team_id

**Files:**
- Modify: `integrations/slack/installation_store.py`
- Test: extend `tests/test_slack_installation_store.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_slack_installation_store.py`:

```python
@pytest.mark.asyncio
async def test_find_bot_returns_decrypted_token():
    store = PostgresInstallationStore()  # no org_id needed for lookup

    row = MagicMock()
    row.bot_token_enc = b"CIPHER"
    row.bot_user_id = "UBOTID"
    row.team_id = "T123"

    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(first=lambda: row)
    )
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "integrations.slack.installation_store.async_session",
        return_value=session_cm,
    ), patch(
        "integrations.slack.installation_store.installation_crypto.decrypt",
        new=AsyncMock(return_value="xoxb-plain"),
    ):
        bot = await store.async_find_bot(team_id="T123")

    assert bot is not None
    assert bot.bot_token == "xoxb-plain"
    assert bot.bot_user_id == "UBOTID"
    assert bot.team_id == "T123"


@pytest.mark.asyncio
async def test_find_bot_returns_none_for_unknown_team():
    store = PostgresInstallationStore()

    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(first=lambda: None))
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "integrations.slack.installation_store.async_session",
        return_value=session_cm,
    ):
        bot = await store.async_find_bot(team_id="T_UNKNOWN")
    assert bot is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_slack_installation_store.py -q`
Expected: 2 of the 3 fail (`NotImplementedError`).

- [ ] **Step 3: Implement `async_find_bot`**

Replace the `raise NotImplementedError` body in `integrations/slack/installation_store.py`:

```python
    async def async_find_bot(
        self,
        *,
        enterprise_id: str | None = None,
        team_id: str | None = None,
        is_enterprise_install: bool | None = None,
    ) -> BotInstallation | None:
        if not team_id:
            return None
        async with async_session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT bot_token_enc, bot_user_id, team_id
                    FROM slack_installations
                    WHERE team_id = :team_id
                    """
                ),
                {"team_id": team_id},
            )
            row = result.first()
            if row is None:
                return None
            bot_token = await installation_crypto.decrypt(
                row.bot_token_enc, session=session
            )
            return BotInstallation(
                bot_token=bot_token,
                bot_user_id=row.bot_user_id,
                team_id=row.team_id,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_slack_installation_store.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/slack/installation_store.py tests/test_slack_installation_store.py
git commit -m "feat(slack): async_find_bot resolves bot token by team_id"
```

---

### Task B3: `async_delete_installation` + helper `find_by_org_id`

**Files:**
- Modify: `integrations/slack/installation_store.py`
- Test: extend `tests/test_slack_installation_store.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_delete_installation_by_team_id():
    store = PostgresInstallationStore()

    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "integrations.slack.installation_store.async_session",
        return_value=session_cm,
    ):
        await store.async_delete_installation(team_id="T123")

    args, _ = session.execute.call_args
    assert "DELETE FROM slack_installations" in str(args[0])


@pytest.mark.asyncio
async def test_find_by_org_id_returns_team_name():
    store = PostgresInstallationStore(org_id=42)

    row = MagicMock(team_id="T123", team_name="acme", bot_user_id="UB")
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(first=lambda: row))
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "integrations.slack.installation_store.async_session",
        return_value=session_cm,
    ):
        info = await store.find_by_org_id(42)
    assert info is not None
    assert info["team_id"] == "T123"
    assert info["team_name"] == "acme"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_slack_installation_store.py -q`
Expected: 2 fail.

- [ ] **Step 3: Implement delete + lookup-by-org**

In `integrations/slack/installation_store.py`, replace the `raise NotImplementedError` for delete and add a new method:

```python
    async def async_delete_installation(
        self,
        *,
        enterprise_id: str | None = None,
        team_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        if not team_id:
            return
        async with async_session() as session:
            await session.execute(
                text("DELETE FROM slack_installations WHERE team_id = :team_id"),
                {"team_id": team_id},
            )
            await session.commit()
        log.info("slack_installation_deleted team_id=%s", team_id)

    async def find_by_org_id(self, org_id: int) -> dict | None:
        """Used by the settings UI to render 'Connected to <workspace>'."""
        async with async_session() as session:
            result = await session.execute(
                text(
                    """
                    SELECT team_id, team_name, bot_user_id
                    FROM slack_installations
                    WHERE org_id = :org_id
                    """
                ),
                {"org_id": org_id},
            )
            row = result.first()
            if row is None:
                return None
            return {
                "team_id": row.team_id,
                "team_name": row.team_name,
                "bot_user_id": row.bot_user_id,
            }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_slack_installation_store.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/slack/installation_store.py tests/test_slack_installation_store.py
git commit -m "feat(slack): async_delete_installation + find_by_org_id"
```

---

## Track C — Slack OAuth endpoints

### Task C1: Admin-only org dependency

**Files:**
- Modify: `orchestrator/auth.py` (existing — Phase 2 added `current_org_id_dep`)
- Test: extend `tests/test_auth.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_auth.py`:

```python
@pytest.mark.asyncio
async def test_current_org_id_admin_dep_blocks_member(monkeypatch):
    """A user with role='member' on an org gets 403 from the admin dep."""
    from fastapi import HTTPException

    from orchestrator.auth import current_org_id_admin_dep

    # We monkeypatch the DB lookup the dep is going to do.
    async def fake_role(*, user_id, org_id):
        return "member"

    monkeypatch.setattr(
        "orchestrator.auth._role_in_org", fake_role, raising=False
    )

    # Build a minimal JWT-payload-shaped dict matching what current_org_id_dep
    # returns (org_id=10, user_id=5)
    with pytest.raises(HTTPException) as exc:
        await current_org_id_admin_dep(org_id=10, user_id=5)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_current_org_id_admin_dep_allows_owner(monkeypatch):
    from orchestrator.auth import current_org_id_admin_dep

    async def fake_role(*, user_id, org_id):
        return "owner"

    monkeypatch.setattr(
        "orchestrator.auth._role_in_org", fake_role, raising=False
    )
    out = await current_org_id_admin_dep(org_id=10, user_id=5)
    assert out == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_auth.py -q`
Expected: 2 new failures — function not exported.

- [ ] **Step 3: Implement the admin dep**

In `orchestrator/auth.py`, alongside the existing `current_user_id` and `current_org_id` deps (lines 75 and 83):

```python
from fastapi import Depends
from sqlalchemy import select

from shared.database import async_session
from shared.models import OrganizationMembership


async def _role_in_org(*, user_id: int, org_id: int) -> str | None:
    async with async_session() as session:
        result = await session.execute(
            select(OrganizationMembership.role).where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.organization_id == org_id,
            )
        )
        return result.scalar_one_or_none()


async def current_org_id_admin_dep(
    user_id: int = Depends(current_user_id),
    org_id: int = Depends(current_org_id),
) -> int:
    """Require the caller to be `owner` or `admin` of the current org."""
    role = await _role_in_org(user_id=user_id, org_id=org_id)
    if role not in ("owner", "admin"):
        raise HTTPException(403, "Org admin role required")
    return org_id
```

Note: `current_user_id` and `current_org_id` are the actual function names in `orchestrator/auth.py` (no `_dep` suffix). The `_dep` alias is added on import — see `orchestrator/orgs.py:18`. New code in `integrations/slack/oauth.py` and `integrations/github/oauth.py` imports the admin dep by its real name:

```python
from orchestrator.auth import current_org_id_admin_dep
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_auth.py -q`
Expected: all auth tests pass (previous suite + 2 new).

- [ ] **Step 5: Commit**

```bash
git add orchestrator/auth.py tests/test_auth.py
git commit -m "feat(auth): current_org_id_admin_dep — owner/admin gate"
```

---

### Task C2: Slack install redirect

**Files:**
- Create: `integrations/slack/oauth.py`
- Test: `tests/test_slack_oauth.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_slack_oauth.py
"""Slack OAuth install flow.

`/api/integrations/slack/install` builds a signed state, then 302s to
slack.com/oauth/v2/authorize with the right scopes and client_id."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from integrations.slack.oauth import router


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "csec")
    monkeypatch.setenv("SLACK_OAUTH_STATE_SECRET", "ssec")
    from shared import config

    config.settings.slack_client_id = "cid"
    config.settings.slack_client_secret = "csec"
    config.settings.slack_oauth_state_secret = "ssec"

    a = FastAPI()
    a.include_router(router)
    return a


@pytest.mark.asyncio
async def test_install_redirects_to_slack_with_state(app, monkeypatch):
    # Bypass the admin dep — return org_id=7 directly.
    from orchestrator import auth

    async def fake_admin(**_kw):
        return 7

    app.dependency_overrides[auth.current_org_id_admin_dep] = fake_admin

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/integrations/slack/install", follow_redirects=False)

    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith("https://slack.com/oauth/v2/authorize")
    assert "client_id=cid" in loc
    assert "state=" in loc
    assert "scope=" in loc  # at least one bot scope
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_slack_oauth.py -q`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Create `integrations/slack/oauth.py` — install endpoint**

```python
# integrations/slack/oauth.py
"""Slack OAuth install + callback + uninstall endpoints.

State CSRF: we sign a JSON payload {"org_id": N, "nonce": <hex>} with
HMAC-SHA256 keyed off SLACK_OAUTH_STATE_SECRET and pass it as the OAuth
`state` parameter. The callback verifies the signature and uses the
embedded org_id directly — we do NOT trust the JWT cookie at callback
time because the user might come back in a different browser window
(rare, but state-signing is cheap insurance).

Scopes requested: chat:write, im:write, im:history, users:read.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets as pysecrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from integrations.slack.installation_store import PostgresInstallationStore
from orchestrator.auth import current_org_id_admin_dep
from shared.config import settings

log = logging.getLogger(__name__)
router = APIRouter()


_SLACK_BOT_SCOPES = ["chat:write", "im:write", "im:history", "users:read"]


def _sign_state(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if not settings.slack_oauth_state_secret:
        raise HTTPException(500, "SLACK_OAUTH_STATE_SECRET not configured")
    mac = hmac.new(
        settings.slack_oauth_state_secret.encode(),
        raw.encode(),
        hashlib.sha256,
    ).hexdigest()
    # base64url so it travels intact in a URL
    import base64
    body = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    return f"{body}.{mac}"


def _verify_state(state: str) -> dict:
    if not settings.slack_oauth_state_secret:
        raise HTTPException(500, "SLACK_OAUTH_STATE_SECRET not configured")
    try:
        body_b64, mac = state.rsplit(".", 1)
        import base64
        padding = "=" * (-len(body_b64) % 4)
        raw = base64.urlsafe_b64decode(body_b64 + padding).decode()
    except Exception:
        raise HTTPException(400, "Malformed state")
    expected = hmac.new(
        settings.slack_oauth_state_secret.encode(),
        raw.encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, mac):
        raise HTTPException(400, "Invalid state signature")
    return json.loads(raw)


@router.get("/api/integrations/slack/install")
async def slack_install(
    org_id: int = Depends(current_org_id_admin_dep),
):
    if not settings.slack_client_id:
        raise HTTPException(500, "SLACK_CLIENT_ID not configured")
    state = _sign_state({"org_id": org_id, "nonce": pysecrets.token_hex(8)})
    qs = urlencode(
        {
            "client_id": settings.slack_client_id,
            "scope": ",".join(_SLACK_BOT_SCOPES),
            "state": state,
        }
    )
    return RedirectResponse(
        url=f"https://slack.com/oauth/v2/authorize?{qs}", status_code=302
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_slack_oauth.py -q`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/slack/oauth.py tests/test_slack_oauth.py
git commit -m "feat(slack-oauth): /install — signed state + 302 to slack.com"
```

---

### Task C3: Slack OAuth callback — token exchange + DB save

**Files:**
- Modify: `integrations/slack/oauth.py`
- Test: extend `tests/test_slack_oauth.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_callback_exchanges_code_and_saves(app, monkeypatch):
    from integrations.slack.oauth import _sign_state

    state = _sign_state({"org_id": 42, "nonce": "abc"})

    fake_slack_response = {
        "ok": True,
        "team": {"id": "T999", "name": "acme"},
        "bot_user_id": "UBOT",
        "access_token": "xoxb-real",
        "authed_user": {"id": "U123"},
    }

    save_mock = AsyncMock()

    async def fake_post(*args, **kwargs):
        m = MagicMock()
        m.json = lambda: fake_slack_response
        m.status_code = 200
        return m

    with patch(
        "integrations.slack.oauth.httpx.AsyncClient.post", new=fake_post
    ), patch.object(PostgresInstallationStore, "async_save", new=save_mock):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.get(
                "/api/integrations/slack/oauth/callback",
                params={"code": "abc", "state": state},
                follow_redirects=False,
            )

    assert resp.status_code == 302
    save_mock.assert_awaited_once()
    install = save_mock.await_args.args[0]
    assert install.team_id == "T999"
    assert install.bot_token == "xoxb-real"
    assert install.bot_user_id == "UBOT"


@pytest.mark.asyncio
async def test_callback_rejects_tampered_state(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        resp = await c.get(
            "/api/integrations/slack/oauth/callback",
            params={"code": "abc", "state": "tampered.bad"},
        )
    assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_slack_oauth.py -q`
Expected: 2 new failures.

- [ ] **Step 3: Implement the callback**

Append to `integrations/slack/oauth.py`:

```python
from types import SimpleNamespace


@router.get("/api/integrations/slack/oauth/callback")
async def slack_oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
):
    payload = _verify_state(state)
    org_id = int(payload["org_id"])

    if not settings.slack_client_id or not settings.slack_client_secret:
        raise HTTPException(500, "Slack OAuth credentials not configured")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://slack.com/api/oauth.v2.access",
            data={
                "client_id": settings.slack_client_id,
                "client_secret": settings.slack_client_secret,
                "code": code,
            },
        )
    body = resp.json()
    if not body.get("ok"):
        log.warning("slack_oauth_exchange_failed body=%s", body)
        raise HTTPException(400, f"Slack OAuth failed: {body.get('error')}")

    install = SimpleNamespace(
        team_id=body["team"]["id"],
        team_name=body["team"].get("name"),
        bot_token=body["access_token"],
        bot_user_id=body["bot_user_id"],
        app_token=None,  # app token is per-app, env-driven
        user_id=body.get("authed_user", {}).get("id"),
    )
    store = PostgresInstallationStore(org_id=org_id)
    await store.async_save(install)

    # Redirect into the settings UI so the user sees connected state.
    return RedirectResponse(
        url="/settings/integrations/slack?connected=1", status_code=302
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_slack_oauth.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/slack/oauth.py tests/test_slack_oauth.py
git commit -m "feat(slack-oauth): callback exchanges code and saves install"
```

---

### Task C4: Slack — GET install state + POST uninstall

**Files:**
- Modify: `integrations/slack/oauth.py`
- Test: extend `tests/test_slack_oauth.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_get_install_returns_connected_state(app, monkeypatch):
    from orchestrator import auth

    async def fake_admin(**_kw):
        return 42

    app.dependency_overrides[auth.current_org_id_admin_dep] = fake_admin

    async def fake_find(self, org_id):
        return {
            "team_id": "T999",
            "team_name": "acme",
            "bot_user_id": "UBOT",
        }

    with patch.object(PostgresInstallationStore, "find_by_org_id", new=fake_find):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.get("/api/integrations/slack")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "connected": True,
        "team_id": "T999",
        "team_name": "acme",
    }


@pytest.mark.asyncio
async def test_get_install_returns_not_connected(app, monkeypatch):
    from orchestrator import auth

    async def fake_admin(**_kw):
        return 42

    app.dependency_overrides[auth.current_org_id_admin_dep] = fake_admin

    async def fake_find(self, org_id):
        return None

    with patch.object(PostgresInstallationStore, "find_by_org_id", new=fake_find):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.get("/api/integrations/slack")
    assert resp.json() == {"connected": False}


@pytest.mark.asyncio
async def test_uninstall_deletes_row(app, monkeypatch):
    from orchestrator import auth

    async def fake_admin(**_kw):
        return 42

    app.dependency_overrides[auth.current_org_id_admin_dep] = fake_admin

    delete_mock = AsyncMock()
    find_mock = AsyncMock(return_value={"team_id": "T999", "team_name": "acme",
                                        "bot_user_id": "UB"})

    with patch.object(PostgresInstallationStore, "async_delete_installation",
                      new=delete_mock), \
         patch.object(PostgresInstallationStore, "find_by_org_id", new=find_mock):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.post("/api/integrations/slack/uninstall")
    assert resp.status_code == 200
    delete_mock.assert_awaited_once_with(team_id="T999")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_slack_oauth.py -q`
Expected: 3 new failures.

- [ ] **Step 3: Implement GET + uninstall**

Append to `integrations/slack/oauth.py`:

```python
@router.get("/api/integrations/slack")
async def slack_install_state(
    org_id: int = Depends(current_org_id_admin_dep),
):
    store = PostgresInstallationStore(org_id=org_id)
    info = await store.find_by_org_id(org_id)
    if info is None:
        return {"connected": False}
    return {
        "connected": True,
        "team_id": info["team_id"],
        "team_name": info["team_name"],
    }


@router.post("/api/integrations/slack/uninstall")
async def slack_uninstall(
    org_id: int = Depends(current_org_id_admin_dep),
):
    store = PostgresInstallationStore(org_id=org_id)
    info = await store.find_by_org_id(org_id)
    if info is None:
        raise HTTPException(404, "Not installed")
    await store.async_delete_installation(team_id=info["team_id"])
    return {"ok": True}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_slack_oauth.py -q`
Expected: 6 passed total.

- [ ] **Step 5: Commit**

```bash
git add integrations/slack/oauth.py tests/test_slack_oauth.py
git commit -m "feat(slack-oauth): GET state + POST uninstall"
```

---

### Task C5: Mount Slack OAuth router + add to scoping allowlist

**Files:**
- Modify: `orchestrator/router.py`
- Modify: `tests/test_org_scoping_coverage.py`

- [ ] **Step 1: Write the failing test**

The scoping coverage test will fail when the new routes are mounted because they aren't on the explicit allowlist. Verify by running:

Run: `.venv/bin/python3 -m pytest tests/test_org_scoping_coverage.py -q`
Expected: still passes (we haven't mounted yet — do this Step 1 now to prove baseline).

Then add to `tests/test_org_scoping_coverage.py::UNSCOPED_ALLOWLIST`:

```python
    # Slack OAuth callback: org_id is embedded in the signed `state`
    # parameter (HMAC), not the JWT cookie.
    ("GET", "/api/integrations/slack/oauth/callback"),
    # GitHub OAuth callback: same — signed state.
    ("GET", "/api/integrations/github/oauth/callback"),
```

- [ ] **Step 2: Mount in `orchestrator/router.py`**

Near the existing `app.include_router(orgs.router)` line (or wherever sub-routers are mounted), add:

```python
from integrations.slack.oauth import router as slack_oauth_router
from integrations.github.oauth import router as github_oauth_router

app.include_router(slack_oauth_router)
app.include_router(github_oauth_router)
```

(`integrations.github.oauth` doesn't exist yet — Track D creates it. Add the import inside an `if False:` block OR defer the import line until after Track D's first task. Recommended: defer — mount only the slack router for now and add the github router import in Track D's mount step.)

For this task, mount only slack:

```python
from integrations.slack.oauth import router as slack_oauth_router

app.include_router(slack_oauth_router)
```

- [ ] **Step 3: Run scoping coverage test**

Run: `.venv/bin/python3 -m pytest tests/test_org_scoping_coverage.py -q`
Expected: pass (the callback is on the allowlist; install / state / uninstall use the admin dep which the static analyzer recognises as scoped).

If the analyzer doesn't recognise `current_org_id_admin_dep` as a scoping dep, extend `_DEP_NAMES` in `test_org_scoping_coverage.py`:

```python
_DEP_NAMES = {"current_org_id_dep", "current_org_id_admin_dep"}
```

- [ ] **Step 4: Run full slack OAuth + coverage suites**

Run: `.venv/bin/python3 -m pytest tests/test_slack_oauth.py tests/test_org_scoping_coverage.py -q`
Expected: 6 + N passed.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/router.py tests/test_org_scoping_coverage.py
git commit -m "feat(slack-oauth): mount router + update scoping coverage"
```

---

## Track D — GitHub App OAuth installation

### Task D1: GitHub install redirect

**Files:**
- Create: `integrations/github/__init__.py` (empty)
- Create: `integrations/github/oauth.py`
- Test: `tests/test_github_oauth.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_github_oauth.py
"""GitHub App OAuth install flow.

`/api/integrations/github/install` 302s to
https://github.com/apps/<slug>/installations/new?state=<signed_state>.
GitHub's flow is simpler than Slack's: the user lands on github.com,
picks repos, and gets bounced back with ?installation_id=N&state=...
We don't exchange a code — GitHub gives us the installation_id directly."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from integrations.github.oauth import router


@pytest.fixture
def app(monkeypatch):
    from shared import config
    config.settings.github_app_slug = "auto-agent"
    config.settings.slack_oauth_state_secret = "ssec"  # reused for github state

    a = FastAPI()
    a.include_router(router)
    return a


@pytest.mark.asyncio
async def test_install_redirects_to_github_app_install_url(app, monkeypatch):
    from orchestrator import auth

    async def fake_admin(**_kw):
        return 7

    app.dependency_overrides[auth.current_org_id_admin_dep] = fake_admin

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        resp = await c.get(
            "/api/integrations/github/install", follow_redirects=False
        )

    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith("https://github.com/apps/auto-agent/installations/new")
    assert "state=" in loc
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_github_oauth.py -q`
Expected: FAIL — `integrations.github.oauth` doesn't exist.

- [ ] **Step 3: Create `integrations/github/__init__.py` + `oauth.py`**

```python
# integrations/github/__init__.py
```

```python
# integrations/github/oauth.py
"""GitHub App OAuth install + uninstall.

The GitHub App install flow does not use OAuth's code-exchange step —
the user clicks "Install" on github.com, GitHub redirects to our
callback with ?installation_id=N&state=..., and we just trust that
installation_id (after verifying our signed `state` to prevent CSRF).

Token minting still goes through shared/github_auth.py — this module
only persists the installation_id and account_login."""
from __future__ import annotations

import logging
import secrets as pysecrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from integrations.slack.oauth import _sign_state, _verify_state  # reuse signing
from orchestrator.auth import current_org_id_admin_dep
from shared.config import settings
from shared.database import async_session

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/integrations/github/install")
async def github_install(
    org_id: int = Depends(current_org_id_admin_dep),
):
    if not settings.github_app_slug:
        raise HTTPException(500, "GITHUB_APP_SLUG not configured")
    state = _sign_state({"org_id": org_id, "nonce": pysecrets.token_hex(8)})
    qs = urlencode({"state": state})
    return RedirectResponse(
        url=f"https://github.com/apps/{settings.github_app_slug}/installations/new?{qs}",
        status_code=302,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_github_oauth.py -q`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/github/__init__.py integrations/github/oauth.py tests/test_github_oauth.py
git commit -m "feat(github-oauth): /install — 302 to github.com app install"
```

---

### Task D2: GitHub OAuth callback — store installation_id + account

**Files:**
- Modify: `integrations/github/oauth.py`
- Test: extend `tests/test_github_oauth.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_callback_stores_installation_id(app, monkeypatch):
    from integrations.slack.oauth import _sign_state

    state = _sign_state({"org_id": 42, "nonce": "abc"})

    # The callback fetches /app/installations/{id} to learn account info.
    # Mock that HTTP call.
    async def fake_get(*args, **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json = lambda: {
            "account": {"login": "acme-inc", "type": "Organization"},
        }
        return m

    # Capture the SQL insert.
    inserted = {}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, sql, params=None):
            inserted["sql"] = str(sql)
            inserted["params"] = params
            return MagicMock()

        async def commit(self):
            return None

    with patch(
        "integrations.github.oauth.httpx.AsyncClient.get", new=fake_get
    ), patch(
        "integrations.github.oauth.async_session", return_value=FakeSession()
    ), patch(
        "integrations.github.oauth._app_jwt_for_install_lookup",
        return_value="JWT",
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.get(
                "/api/integrations/github/oauth/callback",
                params={"installation_id": "12345", "state": state},
                follow_redirects=False,
            )

    assert resp.status_code == 302
    assert "INSERT INTO github_installations" in inserted["sql"]
    assert inserted["params"]["installation_id"] == 12345
    assert inserted["params"]["org_id"] == 42
    assert inserted["params"]["account_login"] == "acme-inc"
    assert inserted["params"]["account_type"] == "Organization"


@pytest.mark.asyncio
async def test_callback_rejects_tampered_state(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        resp = await c.get(
            "/api/integrations/github/oauth/callback",
            params={"installation_id": "1", "state": "bad.state"},
        )
    assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_github_oauth.py -q`
Expected: 2 new failures.

- [ ] **Step 3: Implement the callback**

Append to `integrations/github/oauth.py`:

```python
import time

import jwt as pyjwt


def _app_jwt_for_install_lookup() -> str:
    """Mint a short-lived App JWT to query /app/installations/{id}.

    Same shape as shared.github_auth._build_app_jwt but isolated here so
    this module doesn't depend on github_auth's caching state."""
    if not settings.github_app_id or not settings.github_app_private_key:
        raise HTTPException(500, "GITHUB_APP_ID + private key required")
    now = int(time.time())
    raw_key = settings.github_app_private_key
    if "\\n" in raw_key and "BEGIN" in raw_key:
        raw_key = raw_key.replace("\\n", "\n")
    return pyjwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": settings.github_app_id},
        raw_key,
        algorithm="RS256",
    )


@router.get("/api/integrations/github/oauth/callback")
async def github_oauth_callback(
    installation_id: int = Query(...),
    state: str = Query(...),
):
    payload = _verify_state(state)
    org_id = int(payload["org_id"])

    # Look up the installation to learn account_login + account_type.
    app_jwt = _app_jwt_for_install_lookup()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://api.github.com/app/installations/{installation_id}",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if resp.status_code != 200:
        log.warning(
            "github_install_lookup_failed id=%s status=%s body=%s",
            installation_id, resp.status_code, resp.text[:300],
        )
        raise HTTPException(400, "GitHub installation lookup failed")
    body = resp.json()
    account_login = body["account"]["login"]
    account_type = body["account"].get("type", "Organization")

    async with async_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO github_installations
                    (org_id, installation_id, account_login, account_type,
                     installed_at)
                VALUES
                    (:org_id, :installation_id, :account_login, :account_type,
                     now())
                ON CONFLICT (org_id) DO UPDATE SET
                    installation_id = EXCLUDED.installation_id,
                    account_login = EXCLUDED.account_login,
                    account_type = EXCLUDED.account_type,
                    installed_at = now()
                """
            ),
            {
                "org_id": org_id,
                "installation_id": installation_id,
                "account_login": account_login,
                "account_type": account_type,
            },
        )
        await session.commit()
    log.info(
        "github_installation_saved org_id=%s installation_id=%s login=%s",
        org_id, installation_id, account_login,
    )
    return RedirectResponse(
        url="/settings/integrations/github?connected=1", status_code=302
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_github_oauth.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/github/oauth.py tests/test_github_oauth.py
git commit -m "feat(github-oauth): callback stores installation_id + account"
```

---

### Task D3: GitHub install state + uninstall

**Files:**
- Modify: `integrations/github/oauth.py`
- Test: extend `tests/test_github_oauth.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_get_install_returns_connected(app, monkeypatch):
    from orchestrator import auth

    async def fake_admin(**_kw):
        return 42

    app.dependency_overrides[auth.current_org_id_admin_dep] = fake_admin

    row = MagicMock(
        installation_id=12345, account_login="acme-inc", account_type="Organization"
    )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            return MagicMock(first=lambda: row)

    with patch(
        "integrations.github.oauth.async_session", return_value=FakeSession()
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.get("/api/integrations/github")

    body = resp.json()
    assert body == {
        "connected": True,
        "installation_id": 12345,
        "account_login": "acme-inc",
        "account_type": "Organization",
    }


@pytest.mark.asyncio
async def test_uninstall_deletes_row(app, monkeypatch):
    from orchestrator import auth

    async def fake_admin(**_kw):
        return 42

    app.dependency_overrides[auth.current_org_id_admin_dep] = fake_admin

    calls = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, sql, params=None):
            calls.append(str(sql))
            return MagicMock()

        async def commit(self):
            return None

    with patch(
        "integrations.github.oauth.async_session", return_value=FakeSession()
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.post("/api/integrations/github/uninstall")

    assert resp.status_code == 200
    assert any("DELETE FROM github_installations" in s for s in calls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_github_oauth.py -q`
Expected: 2 new failures.

- [ ] **Step 3: Implement GET + uninstall**

Append to `integrations/github/oauth.py`:

```python
@router.get("/api/integrations/github")
async def github_install_state(
    org_id: int = Depends(current_org_id_admin_dep),
):
    async with async_session() as session:
        result = await session.execute(
            text(
                """
                SELECT installation_id, account_login, account_type
                FROM github_installations
                WHERE org_id = :org_id
                """
            ),
            {"org_id": org_id},
        )
        row = result.first()
    if row is None:
        return {"connected": False}
    return {
        "connected": True,
        "installation_id": row.installation_id,
        "account_login": row.account_login,
        "account_type": row.account_type,
    }


@router.post("/api/integrations/github/uninstall")
async def github_uninstall(
    org_id: int = Depends(current_org_id_admin_dep),
):
    async with async_session() as session:
        await session.execute(
            text("DELETE FROM github_installations WHERE org_id = :org_id"),
            {"org_id": org_id},
        )
        await session.commit()
    log.info("github_installation_deleted org_id=%s", org_id)
    return {"ok": True}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_github_oauth.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/github/oauth.py tests/test_github_oauth.py
git commit -m "feat(github-oauth): GET state + POST uninstall"
```

---

### Task D4: `shared.github_auth` — per-org installation_id resolution

**Files:**
- Modify: `shared/github_auth.py`
- Test: `tests/test_github_auth_per_org_app.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_github_auth_per_org_app.py
"""When an org has a github_installations row, get_github_token mints
against that installation_id — not the global env-driven one."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared import github_auth


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    github_auth.reset_cache()
    # Pretend the env-level App is also configured so we can prove the
    # per-org branch fires before the env-app branch.
    monkeypatch.setattr(github_auth.settings, "github_app_id", "111")
    monkeypatch.setattr(
        github_auth.settings, "github_app_private_key", "-----dummy-----"
    )
    monkeypatch.setattr(
        github_auth.settings, "github_app_installation_id", "999"
    )


@pytest.mark.asyncio
async def test_per_org_installation_id_used_when_present():
    row = MagicMock(installation_id=42)

    async def fake_lookup(*, org_id):
        return row.installation_id if org_id == 7 else None

    async def fake_mint(installation_id):
        # Prove this mint is keyed on the org's installation_id (42), not
        # the env's (999).
        return github_auth._CachedToken(value=f"ghs_for_{installation_id}",
                                        expires_at=1e12)

    with patch(
        "shared.github_auth._installation_id_for_org",
        new=fake_lookup, create=True,
    ), patch(
        "shared.github_auth._mint_installation_token_for", new=fake_mint, create=True,
    ):
        tok = await github_auth.get_github_token(organization_id=7)

    assert tok == "ghs_for_42"


@pytest.mark.asyncio
async def test_falls_back_to_env_when_no_per_org_row():
    async def fake_lookup(*, org_id):
        return None

    async def fake_mint(installation_id):
        return github_auth._CachedToken(
            value=f"ghs_env_{installation_id}", expires_at=1e12
        )

    with patch(
        "shared.github_auth._installation_id_for_org",
        new=fake_lookup, create=True,
    ), patch(
        "shared.github_auth._mint_installation_token_for", new=fake_mint, create=True,
    ):
        tok = await github_auth.get_github_token(organization_id=7)

    assert tok == "ghs_env_999"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_github_auth_per_org_app.py -q`
Expected: 2 failures (functions don't exist).

- [ ] **Step 3: Refactor `shared/github_auth.py`**

This is the biggest change in Track D. We need to:
1. Add `_installation_id_for_org(*, org_id)` that reads `github_installations.installation_id`.
2. Refactor `_mint_installation_token()` → `_mint_installation_token_for(installation_id)` (takes the ID as a parameter rather than reading from settings).
3. Make `_cached` a dict keyed by `installation_id` rather than a single global.
4. In `get_github_token`, insert the per-org branch between user-PAT and env-app:

```python
# Replace existing module-level `_cached: _CachedToken | None`
_cached: dict[int, _CachedToken] = {}


async def _installation_id_for_org(*, org_id: int) -> int | None:
    """Look up the per-org GitHub App installation_id, if any.

    Returns the int installation_id (which we pass to
    /app/installations/{id}/access_tokens), or None if this org hasn't
    installed the App yet — in which case the caller falls back to the
    env-level installation_id."""
    from sqlalchemy import text
    from shared.database import async_session

    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT installation_id FROM github_installations "
                "WHERE org_id = :org_id"
            ),
            {"org_id": org_id},
        )
        row = result.first()
        return row.installation_id if row else None


async def _mint_installation_token_for(installation_id: int) -> _CachedToken:
    """Mint a fresh installation token against the given installation_id."""
    app_jwt = _build_app_jwt()
    url = (
        f"https://api.github.com/app/installations/"
        f"{installation_id}/access_tokens"
    )
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if resp.status_code != 201:
        raise RuntimeError(
            f"GitHub App token mint failed for installation {installation_id}: "
            f"{resp.status_code} {resp.text[:300]}"
        )
    body = resp.json()
    token = body["token"]
    from datetime import datetime
    expires_at = (
        datetime.strptime(body["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=UTC)
        .timestamp()
    )
    return _CachedToken(value=token, expires_at=expires_at)


# Keep _mint_installation_token as a thin shim for the env-level path
# (existing callers that don't supply an installation_id):
async def _mint_installation_token() -> _CachedToken:
    return await _mint_installation_token_for(
        int(settings.github_app_installation_id)
    )
```

Now the resolution function:

```python
async def get_github_token(
    user_id: int | None = None,
    *,
    organization_id: int | None = None,
) -> str:
    """Return a usable GitHub token.

    Resolution order (highest priority first):
        1. user_secrets[user_id, organization_id, "github_pat"]
        2. Per-org github_installations[organization_id] — mint against
           that installation_id (Phase 3 NEW)
        3. Env-level GitHub App installation
        4. Legacy env-var PAT (settings.github_token)
    """
    # 1. Per-user PAT
    if user_id is not None and organization_id is not None:
        try:
            from shared import secrets as _secrets
            pat = await _secrets.get(
                user_id, "github_pat", org_id=organization_id,
            )
        except Exception as e:
            log.warning(
                "github_user_pat_lookup_failed user_id=%s org_id=%s err=%s",
                user_id, organization_id, e,
            )
            pat = None
        if pat:
            _log_mode_once("user_pat")
            return pat

    # 2. Per-org GitHub App installation (NEW in Phase 3)
    if organization_id is not None:
        try:
            install_id = await _installation_id_for_org(org_id=organization_id)
        except Exception as e:
            log.warning(
                "github_org_install_lookup_failed org_id=%s err=%s",
                organization_id, e,
            )
            install_id = None
        if install_id is not None:
            now = time.time()
            cached = _cached.get(install_id)
            if cached is not None and cached.expires_at - now > _REFRESH_MARGIN_SECONDS:
                _log_mode_once("org_app")
                return cached.value
            async with _lock:
                cached = _cached.get(install_id)
                now = time.time()
                if cached is not None and cached.expires_at - now > _REFRESH_MARGIN_SECONDS:
                    _log_mode_once("org_app")
                    return cached.value
                try:
                    cached = await _mint_installation_token_for(install_id)
                    _cached[install_id] = cached
                    _log_mode_once("org_app")
                    return cached.value
                except Exception as e:
                    log.warning(
                        "github_org_app_mint_failed_falling_back_to_env install_id=%s err=%s",
                        install_id, e,
                    )

    # 3. Env-level GitHub App
    if not _app_configured():
        _log_mode_once("env_pat")
        return settings.github_token or ""

    now = time.time()
    env_install_id = int(settings.github_app_installation_id)
    cached = _cached.get(env_install_id)
    if cached is not None and cached.expires_at - now > _REFRESH_MARGIN_SECONDS:
        _log_mode_once("app")
        return cached.value

    async with _lock:
        cached = _cached.get(env_install_id)
        now = time.time()
        if cached is not None and cached.expires_at - now > _REFRESH_MARGIN_SECONDS:
            _log_mode_once("app")
            return cached.value
        try:
            cached = await _mint_installation_token_for(env_install_id)
            _cached[env_install_id] = cached
            _log_mode_once("app")
            return cached.value
        except Exception as e:
            log.warning(
                "github_app_mint_failed_falling_back_to_pat error=%s", e,
            )
            _log_mode_once("env_pat_after_app_failure")
            return settings.github_token or ""


def reset_cache() -> None:
    """Test hook — drop all cached tokens AND the logged-mode memo."""
    global _cached
    _cached = {}
    _logged_modes.clear()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_github_auth_per_org_app.py tests/test_github_auth_per_user.py -q`
Expected: All pass. The per-user test must still pass (we didn't break that path).

- [ ] **Step 5: Commit**

```bash
git add shared/github_auth.py tests/test_github_auth_per_org_app.py
git commit -m "feat(github-auth): per-org installation_id resolution"
```

---

### Task D5: Mount GitHub OAuth router

**Files:**
- Modify: `orchestrator/router.py`

- [ ] **Step 1: Add import + mount**

In `orchestrator/router.py`, alongside the existing slack mount:

```python
from integrations.github.oauth import router as github_oauth_router

app.include_router(github_oauth_router)
```

- [ ] **Step 2: Run full new-router suites**

Run: `.venv/bin/python3 -m pytest tests/test_slack_oauth.py tests/test_github_oauth.py tests/test_org_scoping_coverage.py -q`
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/router.py
git commit -m "feat(github-oauth): mount router"
```

---

## Track E — Slack runtime rewrite (multi-team)

### Task E1: `_get_app()` becomes multi-team with installation store

**Files:**
- Modify: `integrations/slack/main.py`
- Test: `tests/test_slack_multi_team_routing.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_slack_multi_team_routing.py
"""Multi-team Slack: AsyncApp built with installation_store, no static
`token=`. Token resolution at event time goes through async_find_bot."""
from __future__ import annotations

from unittest.mock import patch

from integrations.slack import main as slack_main
from integrations.slack.installation_store import PostgresInstallationStore


def test_get_app_uses_installation_store_when_no_legacy_token(monkeypatch):
    # No legacy token configured → multi-team mode.
    monkeypatch.setattr(slack_main.settings, "slack_bot_token", "")
    slack_main._app = None  # reset singleton

    app = slack_main._get_app()
    assert app.installation_store is not None
    assert isinstance(app.installation_store, PostgresInstallationStore)


def test_get_app_uses_legacy_token_when_set_and_no_installations(monkeypatch):
    """Single-tenant deploys without distributed-app credentials still
    work via the legacy SLACK_BOT_TOKEN env path."""
    monkeypatch.setattr(slack_main.settings, "slack_bot_token", "xoxb-legacy")
    slack_main._app = None

    app = slack_main._get_app()
    # The legacy path uses token=, not installation_store.
    # slack-bolt's AsyncApp doesn't expose this directly; verify the
    # private _token instead.
    assert app._token == "xoxb-legacy" or getattr(app, "token", None) == "xoxb-legacy"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_slack_multi_team_routing.py -q`
Expected: FAIL — current `_get_app` always uses `token=settings.slack_bot_token`.

- [ ] **Step 3: Modify `_get_app()` in `integrations/slack/main.py`**

Replace the existing `_get_app()` body:

```python
_app: AsyncApp | None = None


def _get_app() -> AsyncApp:
    """Build the slack-bolt async app.

    Two modes:
      * Multi-team (default once Phase 3 is rolled out): installation
        store backed by Postgres; bot tokens resolved per-team_id.
      * Legacy single-tenant: settings.slack_bot_token only — used by
        the dev VM until the distributed app is registered.

    The mode is decided lazily on first call. Tests reset _app=None to
    rebuild with different settings.
    """
    global _app
    if _app is not None:
        return _app

    if settings.slack_bot_token:
        # Legacy path — single-workspace deploy. Keep working until the
        # distributed app is registered.
        _app = AsyncApp(token=settings.slack_bot_token)
    else:
        # Phase 3 path — distributed app. signing_secret is optional
        # because we use Socket Mode (no public webhook endpoint).
        from integrations.slack.installation_store import PostgresInstallationStore
        _app = AsyncApp(
            signing_secret=None,
            installation_store=PostgresInstallationStore(),
        )
    return _app
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_slack_multi_team_routing.py -q`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/slack/main.py tests/test_slack_multi_team_routing.py
git commit -m "feat(slack): _get_app() multi-team mode w/ installation_store"
```

---

### Task E2: `send_slack_dm` resolves bot token per org

**Files:**
- Modify: `integrations/slack/main.py`
- Test: extend `tests/test_slack_multi_team_routing.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_send_slack_dm_uses_per_org_bot_token(monkeypatch):
    """In multi-team mode, send_slack_dm(slack_user_id, text, org_id=42)
    fetches org 42's bot_token from the installation store, then posts
    via an AsyncWebClient initialised with that token — NOT via the
    singleton app.client."""
    from slack_sdk.web.async_client import AsyncWebClient

    monkeypatch.setattr(slack_main.settings, "slack_bot_token", "")
    slack_main._app = None

    # Mock the installation store lookup.
    async def fake_find(self, *, team_id=None, **k):
        # Not used in send path; we resolve via org_id.
        return None

    async def fake_find_by_org(self, org_id):
        return {
            "team_id": "T42",
            "team_name": "acme",
            "bot_user_id": "UB42",
        }

    # We also need the decrypted bot token. We stub a new helper
    # `_bot_token_for_org` defined as part of this task.
    async def fake_bot_token(org_id):
        return "xoxb-org42"

    open_resp = MagicMock()
    open_resp.__getitem__ = lambda self, k: {"channel": {"id": "D1"}}[k]
    post_resp = {"ts": "1.0"}

    posts = []

    async def fake_open(users):
        return open_resp

    async def fake_post(channel, text, mrkdwn):
        posts.append({"channel": channel, "text": text, "token_used": "captured-by-client-init"})
        return post_resp

    # Patch AsyncWebClient to record the token it was init'd with.
    original_init = AsyncWebClient.__init__

    init_calls = []

    def capture_init(self, token=None, **kw):
        init_calls.append(token)
        original_init(self, token=token, **kw)
        self.conversations_open = fake_open
        self.chat_postMessage = fake_post

    monkeypatch.setattr(AsyncWebClient, "__init__", capture_init)
    monkeypatch.setattr(
        slack_main, "_bot_token_for_org", fake_bot_token, raising=False
    )

    await slack_main.send_slack_dm(
        "UTARGET", "hello", org_id=42,
    )

    assert init_calls == ["xoxb-org42"]
    assert posts and posts[0]["channel"] == "D1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_slack_multi_team_routing.py::test_send_slack_dm_uses_per_org_bot_token -q`
Expected: FAIL — `org_id` kwarg not supported / `_bot_token_for_org` doesn't exist.

- [ ] **Step 3: Modify `send_slack_dm` + add `_bot_token_for_org`**

In `integrations/slack/main.py`:

```python
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy import text

from shared import installation_crypto
from shared.database import async_session


async def _bot_token_for_org(org_id: int) -> str | None:
    """Resolve a bot token for the given org. Returns the legacy env
    token when no org_id is provided AND settings.slack_bot_token is set
    (single-tenant fallback)."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT bot_token_enc FROM slack_installations "
                "WHERE org_id = :org_id"
            ),
            {"org_id": org_id},
        )
        row = result.first()
        if row is None:
            return None
        return await installation_crypto.decrypt(
            row.bot_token_enc, session=session
        )


async def send_slack_dm(
    slack_user_id: str,
    text_msg: str,
    *,
    task_id: int | None = None,
    org_id: int | None = None,
) -> None:
    """Send a Slack DM to `slack_user_id` in org `org_id`'s workspace.

    Resolution:
      * If `org_id` is set and that org has an installation → per-org bot token.
      * Else if `settings.slack_bot_token` is set → legacy single-tenant.
      * Else: log and bail.
    """
    if not slack_user_id:
        return

    bot_token: str | None = None
    if org_id is not None:
        bot_token = await _bot_token_for_org(org_id)
    if bot_token is None and settings.slack_bot_token:
        bot_token = settings.slack_bot_token
    if not bot_token:
        log.info(
            "send_slack_dm_no_token org_id=%s slack_user_id=%s — "
            "org hasn't installed Slack and no legacy token configured",
            org_id, slack_user_id,
        )
        return

    try:
        client = AsyncWebClient(token=bot_token)
        open_resp = await client.conversations_open(users=slack_user_id)
        channel = open_resp["channel"]["id"]
        post_resp = await client.chat_postMessage(
            channel=channel, text=text_msg, mrkdwn=True
        )
        ts = post_resp.get("ts")
        if task_id is not None and ts:
            from shared.task_channel import task_channel
            await task_channel(task_id).bind_slack_message(ts)
    except Exception:
        log.exception("Failed to send Slack DM")
```

Note: the existing signature took `text` not `text_msg`. Rename it to `text_msg` (or keep `text` and rename the import — pick one). All call sites in `run.py` and elsewhere will need to be updated. **Add a backward-compat shim** by accepting both as positional:

Actually, since `text` is a positional-after-the-id arg today, keep the name `text` and just shadow the SQLAlchemy `text` import locally where needed:

```python
async def send_slack_dm(
    slack_user_id: str,
    text: str,
    *,
    task_id: int | None = None,
    org_id: int | None = None,
) -> None:
    ...
```

Use `sqlalchemy_text` as the alias for `from sqlalchemy import text as sqlalchemy_text` at the module top, OR just inline the SQL string into a `text()` call and accept the name clash inside the function scope (Python resolves the local `text` parameter, but `text()` is called inside `_bot_token_for_org` where the parameter isn't in scope, so this works fine).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_slack_multi_team_routing.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/slack/main.py tests/test_slack_multi_team_routing.py
git commit -m "feat(slack): send_slack_dm resolves bot token per org"
```

---

### Task E3: `_handle_dm_event` resolves `team_id` → `org_id`

**Files:**
- Modify: `integrations/slack/main.py`
- Test: extend `tests/test_slack_multi_team_routing.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_handle_dm_resolves_org_id_from_team(monkeypatch):
    """DM events arrive tagged with `team`/`team_id`. The handler must
    resolve that to an org_id BEFORE looking up the linked user."""
    monkeypatch.setattr(slack_main.settings, "slack_bot_token", "")

    captured_org = {}

    async def fake_org_for_team(team_id):
        return 42 if team_id == "T42" else None

    async def fake_user_for_slack(slack_user_id, *, org_id=None):
        captured_org["org_id"] = org_id
        return {"id": 1, "username": "alice", "display_name": "Alice"}

    async def fake_send(slack_user_id, msg, *, task_id=None, org_id=None):
        return None

    async def fake_converse(slack_user_id, user_id, text, *, org_id=None):
        captured_org["converse_org"] = org_id
        return ""

    monkeypatch.setattr(slack_main, "_org_for_team", fake_org_for_team, raising=False)
    monkeypatch.setattr(slack_main, "_user_for_slack_id", fake_user_for_slack)
    monkeypatch.setattr(slack_main, "send_slack_dm", fake_send)

    # The assistant import happens inside the function; patch by full path.
    import sys, types
    mod = types.ModuleType("agent.slack_assistant")
    mod.converse = fake_converse
    sys.modules["agent.slack_assistant"] = mod

    await slack_main._handle_dm_event(
        {
            "team": "T42",
            "channel_type": "im",
            "user": "U1",
            "text": "hello",
        }
    )
    assert captured_org["org_id"] == 42
    assert captured_org["converse_org"] == 42


@pytest.mark.asyncio
async def test_handle_dm_drops_unknown_team():
    """Events from a workspace we don't have an installation for are dropped."""
    async def fake_org_for_team(team_id):
        return None

    import integrations.slack.main as m
    saved = m._org_for_team if hasattr(m, "_org_for_team") else None
    m._org_for_team = fake_org_for_team
    try:
        # The function should return without raising or contacting Slack.
        result = await m._handle_dm_event(
            {"team": "T_UNKNOWN", "channel_type": "im",
             "user": "U1", "text": "hello"}
        )
        assert result is None
    finally:
        if saved is not None:
            m._org_for_team = saved
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_slack_multi_team_routing.py -q`
Expected: 2 new failures.

- [ ] **Step 3: Modify `_handle_dm_event` and add `_org_for_team`**

In `integrations/slack/main.py`:

```python
async def _org_for_team(team_id: str) -> int | None:
    """Resolve a Slack team_id to an auto-agent org_id. Returns None if
    we don't have an installation for that team."""
    from sqlalchemy import text as _t
    async with async_session() as session:
        result = await session.execute(
            _t(
                "SELECT org_id FROM slack_installations "
                "WHERE team_id = :team_id"
            ),
            {"team_id": team_id},
        )
        row = result.first()
        return row.org_id if row else None
```

Then at the top of `_handle_dm_event`, BEFORE any user lookup:

```python
async def _handle_dm_event(event: dict[str, Any]) -> None:
    if event.get("subtype") or event.get("bot_id"):
        return
    if event.get("channel_type") != "im":
        return
    text_msg: str = (event.get("text") or "").strip()
    if not text_msg:
        return

    # NEW: resolve org_id from team_id. Drop events from unknown teams.
    team_id = event.get("team") or event.get("team_id")
    org_id: int | None = None
    if team_id:
        org_id = await _org_for_team(team_id)
        if org_id is None and not settings.slack_bot_token:
            # Multi-team mode but no install for this team — drop silently.
            log.info("slack_event_dropped_unknown_team team_id=%s", team_id)
            return

    slack_user_id: str = event.get("user", "")
    if not slack_user_id:
        return

    # ... rest of the function, threading org_id into _user_for_slack_id,
    # _autolink_slack_user, send_slack_dm, converse, _post_task_feedback.
```

Then thread `org_id` through every call site inside `_handle_dm_event`:

```python
    if text_msg.lower().split()[0] in ("whoami", "/whoami"):
        await send_slack_dm(
            slack_user_id,
            (
                f"Your Slack user_id is `{slack_user_id}`.\n"
                "Paste this into Settings → Slack in auto-agent to link "
                "your account."
            ),
            org_id=org_id,
        )
        return

    user = await _user_for_slack_id(slack_user_id, org_id=org_id)
    ...
    # converse:
    reply = await converse(
        slack_user_id=slack_user_id,
        user_id=user["id"],
        text=text_msg,
        org_id=org_id,
    )
    if reply:
        await send_slack_dm(slack_user_id, reply, org_id=org_id)
```

`_user_for_slack_id` and `_autolink_slack_user` need org_id kwargs — see Task E4 for the rationale (the same Slack user could exist in two workspaces with different auto-agent identities).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_slack_multi_team_routing.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/slack/main.py tests/test_slack_multi_team_routing.py
git commit -m "feat(slack): _handle_dm_event resolves team_id → org_id"
```

---

### Task E4: `_user_for_slack_id` scoped by org_id

**Files:**
- Modify: `integrations/slack/main.py` (`_user_for_slack_id`, `_autolink_slack_user`)
- Test: add to `tests/test_slack_multi_team_routing.py`

The same Slack `user_id` (a `U…` string) is globally unique within Slack, but in our model a Slack user is linked to an `auto-agent` user. In multi-tenant, the *same* Slack user could be a teammate in two orgs — each with potentially different auto-agent identities. We need org-aware linking: the `User.slack_user_id` column stays, but lookup must filter by org via `OrganizationMembership`.

**Decision:** keep `User.slack_user_id` global (one user = one Slack ID across all their org memberships). The multi-tenant safety is that we only return the user if they're a member of the resolved `org_id`. This means:
- Alice (auto-agent user 5) links her Slack ID `U_ALICE` once.
- Alice joins org A and org B. DMs from `U_ALICE` in workspace A resolve to (Alice, org A). Same DM in workspace B resolves to (Alice, org B).
- The user-linking UI in Slack is per-workspace (the bot DM only exists in workspaces the user is a member of).

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_user_for_slack_id_filters_by_org_membership(monkeypatch):
    """A user must be a member of the org their event came from. If not,
    return None (treat as unlinked) — never silently bind across orgs."""
    from sqlalchemy import text

    # Simulate: User row exists with slack_user_id=U_ALICE; user is NOT
    # a member of org 42.
    captured_sql = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, sql, params=None):
            captured_sql.append(str(sql))
            captured_sql.append(params)
            m = MagicMock()
            m.first = lambda: None  # no row → not a member
            m.scalar_one_or_none = lambda: None
            return m

    monkeypatch.setattr(slack_main, "async_session", lambda: FakeSession())

    user = await slack_main._user_for_slack_id("U_ALICE", org_id=42)
    assert user is None
    joined = " ".join(s for s in captured_sql if isinstance(s, str))
    assert "organization_memberships" in joined or "user_id" in joined
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_slack_multi_team_routing.py::test_user_for_slack_id_filters_by_org_membership -q`
Expected: FAIL — `_user_for_slack_id` doesn't accept `org_id`.

- [ ] **Step 3: Modify `_user_for_slack_id` and `_autolink_slack_user`**

In `integrations/slack/main.py`:

```python
async def _user_for_slack_id(
    slack_user_id: str, *, org_id: int | None = None
) -> dict | None:
    """Look up the auto-agent user linked to a Slack user_id.

    When `org_id` is set, additionally require that the user is a
    member of that org (multi-tenant safety). When `org_id` is None
    (legacy single-tenant path), no membership filter is applied.
    """
    from sqlalchemy import select

    async with async_session() as session:
        from shared.models import OrganizationMembership, User

        if org_id is None:
            result = await session.execute(
                select(User).where(User.slack_user_id == str(slack_user_id))
            )
            user = result.scalar_one_or_none()
        else:
            result = await session.execute(
                select(User)
                .join(
                    OrganizationMembership,
                    OrganizationMembership.user_id == User.id,
                )
                .where(
                    User.slack_user_id == str(slack_user_id),
                    OrganizationMembership.organization_id == org_id,
                )
            )
            user = result.scalar_one_or_none()

        if user is None:
            return None
        return {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
        }
```

And `_autolink_slack_user` similarly gains `org_id` and refuses to link unless the target candidate is a member of `org_id`. Apply the same `OrganizationMembership` filter to each candidate query.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_slack_multi_team_routing.py -q`
Expected: pass (note: the converse helper needs an `org_id` kwarg too — see E5).

- [ ] **Step 5: Commit**

```bash
git add integrations/slack/main.py tests/test_slack_multi_team_routing.py
git commit -m "feat(slack): _user_for_slack_id filters by org membership"
```

---

### Task E5: `converse` accepts an `org_id` kwarg

**Files:**
- Modify: `agent/slack_assistant.py` (signature only — internal HTTP cookie re-issuance is deferred; see Out of scope)
- Test: extend `tests/test_slack_multi_team_routing.py`

`converse` today has signature `async def converse(slack_user_id: str, user_id: int, text: str) -> str` (agent/slack_assistant.py:396). The E3 handler passes `org_id` to it; we add the kwarg now so the caller chain compiles. Deeper per-org system-token re-issuance for the internal HTTP calls is documented as deferred in the Out of scope section — it lands during operational hardening when we seed a real bot user per org. For now, the assistant uses its existing system token; queries it makes are scoped to whatever org the system token carries. Practically: a wrong-org leak through `converse` is mitigated by the upstream filter in `_user_for_slack_id` (Task E4), which already refuses to bind a Slack user who isn't a member of the resolved org.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_converse_accepts_org_id_kwarg():
    """Smoke: the signature includes org_id and the function doesn't crash
    when it receives one. Behavior is unchanged from the existing tests in
    test_slack_assistant.py — this is a signature-extension only."""
    import inspect
    from agent.slack_assistant import converse

    sig = inspect.signature(converse)
    assert "org_id" in sig.parameters
    assert sig.parameters["org_id"].default is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_slack_multi_team_routing.py::test_converse_accepts_org_id_kwarg -q`
Expected: FAIL — `org_id` not a parameter of `converse`.

- [ ] **Step 3: Add the kwarg**

In `agent/slack_assistant.py`, change the signature at line 396:

```python
async def converse(
    slack_user_id: str,
    user_id: int,
    text: str,
    *,
    org_id: int | None = None,
) -> str:
```

Inside the function body, add a single log line so the org_id is observable:

```python
    log.info(
        "slack_converse user_id=%s org_id=%s slack_user_id=%s",
        user_id, org_id, slack_user_id,
    )
```

Do NOT re-issue tokens or change internal HTTP cookie handling — that's the deferred work tracked in Out of scope.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_slack_multi_team_routing.py -q`
Expected: 5 passed (the new signature test + the 4 existing).

- [ ] **Step 5: Commit**

```bash
git add agent/slack_assistant.py tests/test_slack_multi_team_routing.py
git commit -m "feat(slack-assistant): converse accepts org_id kwarg (signature only)"
```

---

### Task E6: Update outbound notification fan-out to pass `org_id`

**Files:**
- Modify: `integrations/slack/main.py` (`notification_loop` formatters)
- Modify: `run.py` if it directly calls `send_slack_dm`
- Test: add a smoke test for the formatter wiring

The Redis-backed `notification_loop` reads events and formats them. Today it calls `send_slack_dm(slack_user_id, text, task_id=task_id)` per event. We need to look up `task.organization_id` and pass it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_slack_notification_loop_org_id.py
"""notification_loop fan-out must pass task.organization_id to
send_slack_dm so the right workspace gets the message."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


@pytest.mark.asyncio
async def test_task_created_event_routes_to_task_org():
    from integrations.slack import main as slack_main

    send_mock = AsyncMock()

    async def fake_fetch_task(task_id):
        return {"id": task_id, "organization_id": 42, "title": "x",
                "repo_id": 1, "created_by_user_id": 1,
                "linked_slack_user_id": "U_OWNER"}

    with patch.object(slack_main, "send_slack_dm", new=send_mock), \
         patch.object(slack_main, "_fetch_task_for_notification",
                      new=fake_fetch_task, create=True):
        await slack_main._notify_task_event(
            event_type="task.created",
            payload={"task_id": 99},
        )
    send_mock.assert_awaited()
    call = send_mock.await_args
    assert call.kwargs.get("org_id") == 42
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_slack_notification_loop_org_id.py -q`
Expected: FAIL — function structure differs.

- [ ] **Step 3: Refactor `notification_loop` to extract a unit-testable `_notify_task_event`**

The exact body depends on the current shape of `notification_loop`. Concretely:
- Pull the inner per-event handler out as `async def _notify_task_event(event_type, payload) -> None`.
- Inside, fetch the task row (helper `_fetch_task_for_notification(task_id)` returning a dict with `organization_id`, `linked_slack_user_id`, `title`, `repo_id`, etc.).
- Call `send_slack_dm(slack_user_id, formatted, task_id=task_id, org_id=task["organization_id"])`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_slack_notification_loop_org_id.py -q`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add integrations/slack/main.py tests/test_slack_notification_loop_org_id.py
git commit -m "feat(slack): notification fan-out passes task.organization_id"
```

---

## Track F — Webhook per-org secret scoping

### Task F1: Resolve org_id from GitHub webhook payload BEFORE signature verify

**Files:**
- Modify: `orchestrator/webhooks/github.py`
- Test: `tests/test_webhook_per_org_secret.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_webhook_per_org_secret.py
"""GitHub webhook signature verification picks the secret based on the
repo's owning org. Allows two orgs to have different secrets without
cross-verification."""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orchestrator.webhooks.github import router


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(router, prefix="/api")
    return a


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()


@pytest.mark.asyncio
async def test_per_org_secret_verifies(app, monkeypatch):
    """org A's secret signs a payload for org A's repo — must verify."""
    payload = {
        "action": "completed",
        "repository": {"full_name": "acme/widgets"},
        "check_suite": {
            "conclusion": "success",
            "pull_requests": [{"html_url": "https://github.com/acme/widgets/pull/1"}],
        },
    }
    body = json.dumps(payload).encode()
    sig = _sign(body, "ORG-A-SECRET")

    async def fake_lookup_secret(*, full_name):
        # Simulates: repo "acme/widgets" → org_id=1, secret="ORG-A-SECRET".
        if full_name == "acme/widgets":
            return "ORG-A-SECRET"
        return None

    handler_called = []

    async def fake_handle_cs(p):
        handler_called.append(p)

    with patch(
        "orchestrator.webhooks.github._secret_for_repo_full_name",
        new=fake_lookup_secret, create=True,
    ), patch(
        "orchestrator.webhooks.github._handle_check_suite",
        new=fake_handle_cs,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.post(
                "/api/webhooks/github",
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "check_suite",
                    "Content-Type": "application/json",
                },
                content=body,
            )
    assert resp.status_code == 200
    assert handler_called


@pytest.mark.asyncio
async def test_per_org_secret_rejects_wrong_secret(app, monkeypatch):
    """Same payload, signed with org B's secret, fails verification
    because the repo belongs to org A."""
    payload = {"repository": {"full_name": "acme/widgets"}, "action": "completed",
               "check_suite": {}}
    body = json.dumps(payload).encode()
    bad_sig = _sign(body, "ORG-B-SECRET")  # WRONG secret for this repo

    async def fake_lookup_secret(*, full_name):
        return "ORG-A-SECRET"  # the repo's actual secret

    with patch(
        "orchestrator.webhooks.github._secret_for_repo_full_name",
        new=fake_lookup_secret, create=True,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.post(
                "/api/webhooks/github",
                headers={
                    "X-Hub-Signature-256": bad_sig,
                    "X-GitHub-Event": "check_suite",
                    "Content-Type": "application/json",
                },
                content=body,
            )
    assert resp.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_webhook_per_org_secret.py -q`
Expected: FAIL — helper doesn't exist, signature uses env-level secret.

- [ ] **Step 3: Modify `orchestrator/webhooks/github.py`**

Add the helper:

```python
from sqlalchemy import text as sa_text

from shared import installation_crypto
from shared.database import async_session


async def _secret_for_repo_full_name(*, full_name: str) -> str | None:
    """Resolve the per-org webhook secret for a repo's full_name.

    full_name: "<owner>/<name>" as GitHub sends it in payloads.

    Looks up the repo by name (the part after the slash matches our
    Repo.name); finds its org_id; reads webhook_secrets[(org_id, "github")].
    Returns None if no per-org override exists — caller falls back to
    settings.github_webhook_secret.
    """
    _, _, name = full_name.partition("/")
    async with async_session() as session:
        # Resolve repo → org_id. Note that Repo.name is unique within an
        # org (Phase 2 composite UNIQUE). full_name disambiguates here.
        result = await session.execute(
            sa_text(
                """
                SELECT ws.secret_enc
                FROM repos r
                JOIN webhook_secrets ws ON ws.org_id = r.organization_id
                                       AND ws.source = 'github'
                WHERE r.name = :name
                LIMIT 1
                """
            ),
            {"name": name},
        )
        row = result.first()
        if row is None:
            return None
        return await installation_crypto.decrypt(
            row.secret_enc, session=session
        )
```

Replace `_verify_signature`:

```python
def _verify_signature_against(payload: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
```

Modify the route handler:

```python
@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header(""),
    x_github_event: str = Header(""),
) -> dict[str, str]:
    body = await request.body()

    # Resolve the per-repo secret (if any) BEFORE verifying signature.
    # We read full_name from the body without trusting it — if the
    # secret matches, that proves the body is untampered.
    try:
        body_json: dict[str, Any] = json.loads(body) if body else {}
    except json.JSONDecodeError:
        body_json = {}
    full_name = (
        body_json.get("repository", {}).get("full_name", "")
    )

    secret: str | None = None
    if full_name:
        try:
            secret = await _secret_for_repo_full_name(full_name=full_name)
        except Exception as e:
            log.warning("webhook_secret_lookup_failed full_name=%s err=%s",
                        full_name, e)
            secret = None
    if not secret:
        secret = settings.github_webhook_secret

    if secret:
        if not _verify_signature_against(body, x_hub_signature_256, secret):
            raise HTTPException(403, "Invalid signature")
    else:
        global _webhook_secret_warned
        if not _webhook_secret_warned:
            log.warning(
                "No webhook secret configured (env or per-org) — "
                "signature verification disabled."
            )
            _webhook_secret_warned = True

    # ... rest unchanged
```

Don't forget `import json` at the top of `orchestrator/webhooks/github.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_webhook_per_org_secret.py -q`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/webhooks/github.py tests/test_webhook_per_org_secret.py
git commit -m "feat(webhooks): per-org github webhook secret lookup"
```

---

### Task F2: Handle `installation.deleted` GitHub event

**Files:**
- Modify: `orchestrator/webhooks/github.py`
- Test: `tests/test_github_installation_deleted.py`

When a customer uninstalls our GitHub App from github.com directly, GitHub fires `installation.deleted`. We should drop the row from `github_installations` so subsequent token mints fail loud rather than try to refresh against a dead installation.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_github_installation_deleted.py
"""installation.deleted event removes the github_installations row."""
from __future__ import annotations
import hashlib, hmac, json
from unittest.mock import patch
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orchestrator.webhooks.github import router


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()


@pytest.fixture
def app(monkeypatch):
    from shared import config
    config.settings.github_webhook_secret = "GLOBAL"
    a = FastAPI()
    a.include_router(router, prefix="/api")
    return a


@pytest.mark.asyncio
async def test_installation_deleted_removes_row(app):
    payload = {
        "action": "deleted",
        "installation": {"id": 12345},
    }
    body = json.dumps(payload).encode()
    sig = _sign(body, "GLOBAL")

    calls = []

    class FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, sql, params=None):
            calls.append((str(sql), params))
            from unittest.mock import MagicMock
            return MagicMock()
        async def commit(self): return None

    with patch(
        "orchestrator.webhooks.github.async_session",
        return_value=FakeSession(),
    ), patch(
        "orchestrator.webhooks.github._secret_for_repo_full_name",
        return_value=None, create=True,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.post(
                "/api/webhooks/github",
                headers={
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Event": "installation",
                    "Content-Type": "application/json",
                },
                content=body,
            )

    assert resp.status_code == 200
    sqls = [c[0] for c in calls]
    assert any("DELETE FROM github_installations" in s for s in sqls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_github_installation_deleted.py -q`
Expected: FAIL — no `installation` event handler.

- [ ] **Step 3: Add the event branch and handler**

In `orchestrator/webhooks/github.py`, inside `github_webhook`:

```python
    elif x_github_event == "installation":
        await _handle_installation_event(payload)
```

And the handler:

```python
async def _handle_installation_event(payload: dict[str, Any]) -> None:
    action = payload.get("action")
    install = payload.get("installation", {})
    install_id = install.get("id")
    if not install_id:
        return
    if action in ("deleted", "suspend"):
        async with async_session() as session:
            await session.execute(
                sa_text(
                    "DELETE FROM github_installations "
                    "WHERE installation_id = :install_id"
                ),
                {"install_id": int(install_id)},
            )
            await session.commit()
        log.info("github_installation_uninstalled installation_id=%s", install_id)
    # 'created' / 'new_permissions_accepted' / 'unsuspend' → no-op;
    # our row is created via the OAuth callback, not via webhook.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_github_installation_deleted.py -q`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/webhooks/github.py tests/test_github_installation_deleted.py
git commit -m "feat(webhooks): handle installation.deleted → drop row"
```

---

## Track G — Settings UI

The four UI files mirror the established pattern in `web-next/app/(app)/settings/`. Each is small (~50 lines) and self-contained.

### Task G1: Integration hub page + sidebar entry

**Files:**
- Create: `web-next/app/(app)/settings/integrations/page.tsx`
- Modify: `web-next/app/(app)/settings/layout.tsx` (add "Integrations" to nav)

- [ ] **Step 1: Add hub page**

```tsx
// web-next/app/(app)/settings/integrations/page.tsx
import Link from "next/link";

export default function IntegrationsHub() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Integrations</h1>
      <p className="text-muted-foreground">
        Connect your organisation to Slack, GitHub, and other services.
      </p>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Card href="/settings/integrations/slack"
              title="Slack"
              desc="Install our bot in your workspace." />
        <Card href="/settings/integrations/github"
              title="GitHub"
              desc="Grant repo access via our GitHub App." />
      </div>
    </div>
  );
}

function Card({ href, title, desc }: { href: string; title: string; desc: string }) {
  return (
    <Link href={href}
          className="block rounded-lg border p-4 hover:bg-accent transition-colors">
      <h2 className="text-lg font-medium">{title}</h2>
      <p className="text-sm text-muted-foreground mt-1">{desc}</p>
    </Link>
  );
}
```

- [ ] **Step 2: Modify settings layout**

In `web-next/app/(app)/settings/layout.tsx`, find the existing sidebar/nav array and append:

```ts
{ href: "/settings/integrations", label: "Integrations" },
```

- [ ] **Step 3: Type-check**

Run: `cd web-next && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add web-next/app/\(app\)/settings/integrations/page.tsx web-next/app/\(app\)/settings/layout.tsx
git commit -m "feat(ui): integrations hub page + sidebar entry"
```

---

### Task G2: Typed fetch helpers + TanStack hooks

**Files:**
- Create: `web-next/lib/integrations.ts`
- Create: `web-next/hooks/useIntegrations.ts`

- [ ] **Step 1: Create lib + hooks**

```ts
// web-next/lib/integrations.ts
export type SlackInstall =
  | { connected: false }
  | { connected: true; team_id: string; team_name: string | null };

export type GitHubInstall =
  | { connected: false }
  | {
      connected: true;
      installation_id: number;
      account_login: string;
      account_type: "User" | "Organization";
    };

export async function fetchSlackInstall(): Promise<SlackInstall> {
  const r = await fetch("/api/integrations/slack", { credentials: "include" });
  if (!r.ok) throw new Error("failed to load slack install");
  return r.json();
}

export async function fetchGitHubInstall(): Promise<GitHubInstall> {
  const r = await fetch("/api/integrations/github", { credentials: "include" });
  if (!r.ok) throw new Error("failed to load github install");
  return r.json();
}

export async function uninstallSlack(): Promise<void> {
  const r = await fetch("/api/integrations/slack/uninstall", {
    method: "POST",
    credentials: "include",
  });
  if (!r.ok) throw new Error("uninstall failed");
}

export async function uninstallGitHub(): Promise<void> {
  const r = await fetch("/api/integrations/github/uninstall", {
    method: "POST",
    credentials: "include",
  });
  if (!r.ok) throw new Error("uninstall failed");
}
```

```ts
// web-next/hooks/useIntegrations.ts
"use client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  fetchGitHubInstall,
  fetchSlackInstall,
  uninstallGitHub,
  uninstallSlack,
} from "@/lib/integrations";

export function useSlackInstall() {
  return useQuery({ queryKey: ["integrations", "slack"], queryFn: fetchSlackInstall });
}

export function useGitHubInstall() {
  return useQuery({
    queryKey: ["integrations", "github"],
    queryFn: fetchGitHubInstall,
  });
}

export function useUninstallSlack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: uninstallSlack,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["integrations", "slack"] }),
  });
}

export function useUninstallGitHub() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: uninstallGitHub,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["integrations", "github"] }),
  });
}
```

- [ ] **Step 2: Type-check**

Run: `cd web-next && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add web-next/lib/integrations.ts web-next/hooks/useIntegrations.ts
git commit -m "feat(ui): typed integrations client + tanstack hooks"
```

---

### Task G3: Slack integration page

**Files:**
- Create: `web-next/app/(app)/settings/integrations/slack/page.tsx`

- [ ] **Step 1: Create the page**

```tsx
// web-next/app/(app)/settings/integrations/slack/page.tsx
"use client";

import { Button } from "@/components/ui/button";
import { useSlackInstall, useUninstallSlack } from "@/hooks/useIntegrations";

export default function SlackIntegrationPage() {
  const { data, isLoading } = useSlackInstall();
  const uninstall = useUninstallSlack();

  if (isLoading) return <p>Loading…</p>;

  if (!data?.connected) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-semibold">Slack</h1>
        <p className="text-muted-foreground">
          Install our bot in your Slack workspace so your team can DM tasks
          to auto-agent.
        </p>
        <a
          href="/api/integrations/slack/install"
          className="inline-block rounded-md bg-primary text-primary-foreground px-4 py-2"
        >
          Add to Slack
        </a>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Slack</h1>
      <p>
        Connected to <strong>{data.team_name ?? data.team_id}</strong>.
      </p>
      <Button
        variant="destructive"
        onClick={() => uninstall.mutate()}
        disabled={uninstall.isPending}
      >
        Disconnect
      </Button>
    </div>
  );
}
```

- [ ] **Step 2: Type-check**

Run: `cd web-next && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add web-next/app/\(app\)/settings/integrations/slack/page.tsx
git commit -m "feat(ui): /settings/integrations/slack page"
```

---

### Task G4: GitHub integration page

**Files:**
- Create: `web-next/app/(app)/settings/integrations/github/page.tsx`

- [ ] **Step 1: Create the page**

```tsx
// web-next/app/(app)/settings/integrations/github/page.tsx
"use client";

import { Button } from "@/components/ui/button";
import { useGitHubInstall, useUninstallGitHub } from "@/hooks/useIntegrations";

export default function GitHubIntegrationPage() {
  const { data, isLoading } = useGitHubInstall();
  const uninstall = useUninstallGitHub();

  if (isLoading) return <p>Loading…</p>;

  if (!data?.connected) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-semibold">GitHub</h1>
        <p className="text-muted-foreground">
          Install the auto-agent GitHub App on your org or user account
          to grant access to the repos you want our bot to work on.
        </p>
        <a
          href="/api/integrations/github/install"
          className="inline-block rounded-md bg-primary text-primary-foreground px-4 py-2"
        >
          Install GitHub App
        </a>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">GitHub</h1>
      <p>
        Installed on{" "}
        <strong>
          {data.account_login} ({data.account_type})
        </strong>
        .
      </p>
      <Button
        variant="destructive"
        onClick={() => uninstall.mutate()}
        disabled={uninstall.isPending}
      >
        Uninstall
      </Button>
    </div>
  );
}
```

- [ ] **Step 2: Type-check**

Run: `cd web-next && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add web-next/app/\(app\)/settings/integrations/github/page.tsx
git commit -m "feat(ui): /settings/integrations/github page"
```

---

## Track H — Final verification + handover

### Task H1: Full unit suite + ruff

- [ ] **Step 1: Run the full test suite**

Run: `.venv/bin/python3 -m pytest tests/ -q`
Expected: all pass. Phase 2 baseline was 762; Phase 3 adds ~25 new tests across `test_models_integrations`, `test_installation_crypto`, `test_slack_installation_store`, `test_slack_oauth`, `test_github_oauth`, `test_github_auth_per_org_app`, `test_slack_multi_team_routing`, `test_webhook_per_org_secret`, `test_github_installation_deleted`, `test_config_phase3`, `test_slack_notification_loop_org_id`, plus 2 new in `test_auth`. Target: **~790 pass**.

- [ ] **Step 2: Ruff**

Run: `ruff check . && ruff format --check .`
Expected: clean. Fix any issues with `ruff check --fix .` and `ruff format .`.

- [ ] **Step 3: TypeScript check**

Run: `cd web-next && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 4: Commit any lint fixes**

```bash
git commit -am "chore(lint): ruff cleanups for Phase 3"  # if any
```

---

### Task H2: PR description + handover doc

**Files:**
- Create: `docs/superpowers/plans/2026-05-11-handover-after-phase-3.md`

The handover should mirror `2026-05-11-handover-after-phase-2.md` and document:

1. **What's done** — schema (028), three new models, Slack OAuth (install/callback/state/uninstall), GitHub App OAuth (install/callback/state/uninstall), per-org `get_github_token` resolution, multi-team `_get_app`, per-org `send_slack_dm`, org-filtered DM handler, webhook per-org secret resolution, `installation.deleted` handler, full settings UI.
2. **Production state** — env vars to add (`SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_OAUTH_STATE_SECRET`, `GITHUB_APP_SLUG`). Slack app must be registered as distributed before merge. GitHub App webhook must include `installation` event.
3. **What's NOT done** — open PR review; phase 2 ops items still pending; Linear per-org webhook (Phase 3.5 / Phase 4); per-org Socket Mode app tokens (defer — one app token works app-wide); testcontainers isolation suite still deferred.
4. **Critical things to know:**
   - Legacy `SLACK_BOT_TOKEN` still works as single-tenant fallback. Once distributed app is registered, remove from prod `.env`.
   - `_cached` in `shared/github_auth.py` is now a `dict[int, _CachedToken]` keyed on `installation_id`. Tests that touched this must use `reset_cache()`.
   - Webhook secret resolution reads `payload.repository.full_name` BEFORE signature verify — this is safe because mismatched secrets fail verification.
   - `OrganizationMembership.role` check is the gate for install/uninstall; non-admin members are 403'd.
5. **Where to find things** — table of paths.

- [ ] **Step 1: Write the handover**

(Copy the structure from `2026-05-11-handover-after-phase-2.md` and fill in the Phase 3 specifics. About 200 lines.)

- [ ] **Step 2: Open the PR**

```bash
git push -u origin feat/phase-3-per-org-integrations
gh pr create --title "Phase 3: per-org Slack + GitHub OAuth" --body "$(cat <<'EOF'
## Summary
- Migration 028: `slack_installations`, `github_installations`, `webhook_secrets` (pgcrypto-encrypted tokens).
- Per-org Slack OAuth (install/callback/state/uninstall) using slack-bolt's multi-team mode.
- Per-org GitHub App OAuth (install/callback/state/uninstall) with `installation.deleted` cleanup.
- `get_github_token` now resolves per-org installation_id before falling back to env-level App.
- `send_slack_dm` resolves the right bot token by `org_id`; `_handle_dm_event` drops events from unknown teams.
- GitHub webhook signature verification now picks the secret based on `repository.full_name → org`.
- Settings UI: `/settings/integrations/{slack,github}`.

## Deploy checklist
- Apply migration 028 (verify SECRETS_PASSPHRASE unchanged from Phase 2).
- Add `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_OAUTH_STATE_SECRET`, `GITHUB_APP_SLUG` to `.env`.
- Register Slack app as "distributed" in api.slack.com/apps. Redirect URL: `https://<domain>/api/integrations/slack/oauth/callback`.
- GitHub App: ensure webhook URL is `https://<domain>/api/webhooks/github` and `installation` events are subscribed.

## Test plan
- 790+ unit tests pass.
- Smoke: org A installs Slack + GitHub → creates a task → notification arrives in org A's workspace; PR commit lands on org A's GitHub repo.
- Cross-org: org B's events don't leak into org A's WS / Slack.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Commit handover**

```bash
git add docs/superpowers/plans/2026-05-11-handover-after-phase-3.md
git commit -m "docs(handover): post-Phase-3 handover for next session"
git push
```

---

## Summary

Phase 3 layers per-org Slack + GitHub OAuth on top of Phase 2's org-scoped data model. The migration adds three encrypted-token tables, the Slack rewrite swaps the singleton `AsyncApp(token=...)` for a multi-team app backed by a Postgres installation store, and `shared.github_auth` learns to mint installation tokens against per-org installation_ids before falling back to the env-level App. Legacy single-tenant deployments keep working through fallback paths in both `_get_app()` (uses `slack_bot_token` if set) and `get_github_token` (env App / env PAT). The settings UI gains a `/settings/integrations` hub with per-service install/disconnect flows.

## Test plan

Beyond the unit suite, manual verification at deploy time:

1. **Two-org Slack isolation** — Org A installs to workspace W1, Org B installs to workspace W2. A DM in W1 from a member of org A produces a task tagged with `organization_id = A`. A DM in W2 produces a task tagged with B. Neither org sees the other's task in `/tasks`.
2. **Two-org GitHub isolation** — Both orgs install the GitHub App on different GitHub accounts. A task in org A produces a PR on org A's repo using org A's installation token. The `github_auth_mode=org_app` log line includes the installation_id from `github_installations[org_id=A]`.
3. **Uninstall cleanup** — Customer uninstalls the GitHub App from github.com → `installation.deleted` webhook fires → row removed → next task in that org transitions to `BLOCKED_ON_AUTH`.
4. **Webhook secret rotation per org** — Org A sets a per-org webhook secret via DB (no UI yet); incoming webhook signed with the global env secret is rejected for org A's repos, accepted with the per-org secret. (Defer the UI for setting per-org webhook secrets — Phase 4.)

## Out of scope (deferred)

- **Linear per-org webhook secret** — same machinery as GitHub but Linear webhooks are lower volume and only a few orgs use Linear today. Phase 3.5 or Phase 4.
- **Per-org Socket Mode app tokens** — slack-bolt's multi-team mode uses one app-level app_token, so this is not needed. Documented for clarity.
- **UI for setting per-org webhook secrets** — currently DB-only. UI form lands in Phase 4 alongside quota config.
- **Testcontainers-backed real-DB isolation suite** — still recommended; the static-analysis coverage test in Phase 2 catches endpoint-level slips, but Phase 3 introduces enough cross-module wiring that a real-DB suite would catch query-level slips. Estimated 1-2 days. Land before Phase 4.
- **Per-org concurrency caps / quota enforcement** — Phase 4.
- **GitHub App permissions UI** — admin sees account_login but no way to inspect the granted permissions or expand them. GitHub's "Configure" link is sufficient for v1.
- **Slack DM-to-task assistant in multi-tenant** — `agent/slack_assistant.py` was tweaked to pass `org_id` through internal HTTP calls. The auth model still uses a system token that resolves to user_id=1 by default; if a customer's bot operates with elevated privileges via that token, audit before going to GA. Hardening tracked in Phase 6 (operational).
