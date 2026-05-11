# Handover — multi-tenant SaaS, after Phase 3

**Date:** 2026-05-11
**Branch:** `feat/phase-3-per-org-integrations` — **PR: TBD** (will be updated once PR is opened)
**Phase 2 status:** merged to `main` at `9a5c2c1` (PR #39). Phase 3 is NOT yet deployed.
**Baseline:** 762 tests passing at Phase 2 merge. Phase 3 adds 46 → **808 tests, 0 failures**.

---

## What's done

### Track A — Schema & foundations

- **Migration 028** (`migrations/versions/028_per_org_integrations.py`): creates three tables:
  - `slack_installations` — 1:1 per org, keyed by `team_id`. Columns: `bot_token_enc BYTEA` (pgcrypto), `bot_user_id`, `app_id`, `org_id FK`.
  - `github_installations` — 1:1 per org, keyed by `installation_id`. Columns: `account_login`, `account_type`, `org_id FK`.
  - `webhook_secrets` — composite PK `(org_id, source)`. Column: `secret_enc BYTEA` (pgcrypto).
  - Idempotently runs `CREATE EXTENSION IF NOT EXISTS pgcrypto` before the table DDL.
- **Three ORM models** added to `shared/models.py`: `SlackInstallation`, `GitHubInstallation`, `WebhookSecret`.
- **`shared/installation_crypto.py`**: `encrypt_token(plaintext)` / `decrypt_token(ciphertext)` using `pgp_sym_encrypt`/`pgp_sym_decrypt` with `SECRETS_PASSPHRASE`. Same passphrase as `shared/secrets.py`, different table, separate module.
- **`shared/config.py`**: four new optional fields — `slack_client_id`, `slack_client_secret`, `slack_oauth_state_secret`, `github_app_slug`. All optional so existing deploys don't break.

### Track B — Slack installation store

- **`integrations/slack/installation_store.py::PostgresInstallationStore`**: implements slack-bolt's `AsyncInstallationStore` contract:
  - `async_save(installation)`: upserts on `team_id`, encrypts `bot_token` via `installation_crypto`.
  - `async_find_bot(enterprise_id, team_id)`: decrypts and returns a `Bot` object; returns `None` if not found.
  - `async_delete_installation(enterprise_id, team_id)`: hard-deletes the row.
  - `find_by_org_id(org_id)`: non-bolt helper for the settings UI — returns the `SlackInstallation` row or `None`.

### Track C — Slack OAuth endpoints

- **`orchestrator/auth.py::current_org_id_admin_dep`**: new FastAPI dependency that gates routes to org owner or admin only. Raises 403 otherwise.
- **`integrations/slack/oauth.py`** — 4 endpoints (NO `/api` prefix in decorators; mount adds it):
  - `GET /integrations/slack/install` (admin-gated): builds OAuth URL with signed HMAC-SHA256 state, redirects 302.
  - `GET /integrations/slack/oauth/callback`: verifies state, exchanges code via slack-bolt OAuth handler, saves via store, redirects to `/settings/integrations/slack`.
  - `GET /integrations/slack`: returns `{connected: bool, team_name?, team_id?}` for UI.
  - `POST /integrations/slack/uninstall`: admin-gated, deletes the row via `async_delete_installation`.
- `_sign_state(org_id)` / `_verify_state(state)` HMAC helpers using `SLACK_OAUTH_STATE_SECRET`.
- Mounted in `run.py` with `prefix="/api"` (commit `0ddd72c`).
- **`tests/test_org_scoping_coverage.py`** updated: recognizes `current_org_id_admin_dep` as a valid org-scope gate; allowlists `/integrations/slack/oauth/callback` (org_id comes from signed state, not JWT).

### Track D — GitHub App OAuth

- **`integrations/github/oauth.py`** — 4 endpoints mirroring Slack's flow (NO code-exchange step; GitHub gives `installation_id` directly):
  - `GET /integrations/github/install` (admin-gated): 302 to `github.com/apps/<GITHUB_APP_SLUG>/installations/new` with signed state.
  - `GET /integrations/github/oauth/callback`: verifies state, fetches installation details via App JWT (`get_app_jwt()`), upserts into `github_installations`.
  - `GET /integrations/github`: returns `{connected: bool, account_login?, installation_id?}`.
  - `POST /integrations/github/uninstall`: admin-gated, hard-deletes the row.
- Mounted in `run.py`. Scoping coverage allowlist extended to include `/integrations/github/oauth/callback`.
- **`shared/github_auth.py` refactor**: `_cached` changed from `_CachedToken | None` to `dict[int, _CachedToken]` keyed by `installation_id`. Resolution order:
  1. Per-user PAT (when both `user_id` and `organization_id` supplied) — Phase 1 path.
  2. Per-org installation_id from `github_installations` — **NEW Phase 3**.
  3. Env-level GitHub App (legacy).
  4. Env-level PAT (legacy fallback).
- New log value `github_auth_mode=org_app` distinguishes per-org from env-level App installs.

### Track E — Slack runtime rewrite (multi-team)

- **`integrations/slack/main.py::_get_app()`**: builds a multi-team `AsyncApp(installation_store=PostgresInstallationStore())` when `settings.slack_bot_token` is empty. Legacy single-tenant path (env token) retained for backward compat.
- **`send_slack_dm(slack_user_id, text, *, task_id=None, org_id=None)`**: resolves bot token via `_bot_token_for_org(org_id)` first, falls back to env token, logs and returns silently when neither is available. Uses a fresh `AsyncWebClient(token=...)` per call rather than the singleton app's client.
- **`_handle_dm_event`**: resolves `team_id → org_id` via `_org_for_team(team_id)` BEFORE any user lookup. Drops events from unknown teams in multi-team mode. Threads `org_id` through `_user_for_slack_id`, `_autolink_slack_user`, `send_slack_dm`, and `converse`.
- **`_user_for_slack_id`** and **`_autolink_slack_user`**: when `org_id` is set, JOIN `organization_memberships` on `user_id` AND filter by `OrganizationMembership.org_id == org_id`. The same Slack user ID in two orgs resolves to distinct accounts; cross-org silently returns `None`.
- **`agent/slack_assistant.py::converse`**: accepts `org_id: int | None = None` kwarg; logs it for observability. Deeper system-token-per-org refactoring is deferred (see "Out of scope").
- **`notification_loop`**: extracted `_notify_task_event` and `_fetch_task_for_notification` helpers; task events now resolve `task.organization_id` and pass it to `send_slack_dm`.

### Track F — Webhook per-org secrets

- **`orchestrator/webhooks/github.py`**: signature verification resolves per-org webhook secret via `_secret_for_repo_full_name(full_name=...)` — JOINs `repos → webhook_secrets`. Falls back to env-level `settings.github_webhook_secret` when no per-org row exists.
- **New `installation` event handler**: on `installation.deleted` or `installation.suspend`, DELETE the `github_installations` row so subsequent token mints fail fast rather than returning a stale token.

### Track G — Settings UI (`web-next/`)

- **`web-next/lib/integrations.ts`**: typed fetch helpers + `SlackInstall` / `GitHubInstall` discriminated unions.
- **`web-next/hooks/useIntegrations.ts`**: TanStack Query hooks: `useSlackInstall`, `useGitHubInstall`, `useUninstallSlack`, `useUninstallGitHub`.
- **`web-next/app/(app)/settings/integrations/page.tsx`**: hub page with grid of integration cards.
- **`web-next/app/(app)/settings/integrations/slack/page.tsx`**: install/disconnect Slack UI.
- **`web-next/app/(app)/settings/integrations/github/page.tsx`**: install/disconnect GitHub App UI.
- **Sidebar nav** extended with "Integrations" entry.

### Tests — +46 new, 808/808 passing

| File | Count | What it covers |
|---|---|---|
| `tests/test_models_integrations.py` | 6 | ORM model introspection (columns, types, FKs) |
| `tests/test_installation_crypto.py` | 4 | encrypt/decrypt round-trip; wrong-passphrase raises |
| `tests/test_config_phase3.py` | 2 | Optional fields default to `None`; set when env present |
| `tests/test_slack_installation_store.py` | 5 | `async_save`, `async_find_bot`, `async_delete_installation`, `find_by_org_id` |
| `tests/test_auth.py` | 3 | `current_org_id_admin_dep` — owner passes, member fails, anon fails |
| `tests/test_slack_oauth.py` | 6 | `/install` 302, state HMAC, callback saves row, `/integrations/slack` state, uninstall |
| `tests/test_github_oauth.py` | 5 | `/install` 302, callback upserts, state endpoint, uninstall idempotent |
| `tests/test_github_auth_per_org_app.py` | 2 | Per-org resolution order; `org_app` log mode |
| `tests/test_slack_multi_team_routing.py` | 7 | Unknown team dropped; known team resolves org_id; cross-org user returns None |
| `tests/test_slack_notification_loop_org_id.py` | 3 | `send_slack_dm` uses per-org token; falls back to env token |
| `tests/test_webhook_per_org_secret.py` | 2 | Per-org secret lookup hit + fallback to env |
| `tests/test_github_installation_deleted.py` | 1 | `installation.deleted` event deletes the DB row |

---

## Production state

**NOT YET DEPLOYED.** Phase 2 (PR #39) is on `main` but Phase 3 has not been pushed to the VM.

**Env vars required before deploy:**

| Var | Notes |
|---|---|
| `SLACK_CLIENT_ID` | From api.slack.com/apps → Basic Information |
| `SLACK_CLIENT_SECRET` | Same location |
| `SLACK_OAUTH_STATE_SECRET` | Generate: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `GITHUB_APP_SLUG` | e.g. `auto-agent` — appears in the App's public URL |
| `SECRETS_PASSPHRASE` | **Must be UNCHANGED** from Phase 1/2. All stored encrypted tokens are keyed to this value. |

**Slack app registration changes required at api.slack.com/apps:**
- Switch app to **Distributed** (Settings → Manage Distribution → "Remove Hard Coded Information" → activate).
- Add OAuth redirect URL: `https://<domain>/api/integrations/slack/oauth/callback`.
- Bot token scopes: `chat:write`, `im:write`, `im:history`, `users:read`.

**GitHub App changes required:**
- Webhook URL: `https://<domain>/api/webhooks/github`.
- Subscribe to the **`installation`** event (so `installation.deleted` fires on uninstall).

**Alembic:**
```
docker compose exec auto-agent alembic upgrade head
```
Verify `alembic current` shows `028`.

---

## What's NOT done (priority order)

1. **PR review + merge** — handover was written immediately after passing tests; reviewer has not seen the code.
2. **Live VM deploy** — alembic 028 upgrade, env-var additions, Slack app re-registration as distributed, GitHub App webhook subscription to `installation` event.
3. **Phase 2 deploy verification** — STILL pending. Carried forward from the Phase 2 handover. Do this before deploying Phase 3 on top (see Phase 2 handover for the checklist).
4. **Real end-to-end smoke** — post-deploy, install Slack from one org's Settings page and verify a task notification lands in the correct Slack workspace and NOT in another org's.

---

## Critical things to know

### Route paths use NO `/api` prefix in decorators

`integrations/slack/oauth.py` and `integrations/github/oauth.py` declare routes like `@router.get("/integrations/slack/install")`. The mount in `run.py` adds `prefix="/api"`. Effective path: `/api/integrations/slack/install`. This bit us once (C5 fix, commit `f874cb8`) — if routes appear as 404, check for double-prefix.

### `OrganizationMembership.org_id` — NOT `organization_id`

The column on `OrganizationMembership` is named `org_id` (see `shared/models.py`). Queries that use `organization_id` will silently produce wrong results (no column-not-found error because SQLAlchemy will ignore it as a kwarg filter). Always spell it `org_id`.

### Legacy single-tenant paths are intentionally retained

The existing `settings.slack_bot_token` (env-level) and env-level GitHub App still work. Phase 3 layers per-org on top without removing the legacy paths. Once you install the distributed Slack app for an org, that org's events use the per-org token. The env token becomes unused but harmless.

### `converse` accepts `org_id` but does NOT thread it into internal HTTP calls

`agent/slack_assistant.py::converse(org_id=...)` logs the `org_id` but does not yet route API calls through a per-org system token. The mitigation: `_user_for_slack_id` (called in `_handle_dm_event` BEFORE `converse`) refuses to bind a Slack user who is not a member of the resolved org. Cross-org leakage via `converse` is blocked at the user-lookup boundary, not inside `converse` itself.

### GitHub webhook secret resolution reads unsigned body

`_secret_for_repo_full_name` reads `repository.full_name` from the raw request body to pick the right org's secret. This is safe: if an attacker sends a fake `full_name` pointing to a different org's secret, the HMAC verification still fails because the body content won't match the expected signature.

### WebSocket scoping unchanged from Phase 2

This phase did NOT touch `web/main.py`. Per-client `current_org_id` broadcast filtering is exactly as Phase 2 left it.

---

## Where to find things

| Topic | Path |
|---|---|
| Phase 3 implementation plan | `docs/superpowers/plans/2026-05-11-phase-3-per-org-integrations.md` |
| Full multi-tenant plan | `docs/superpowers/plans/2026-05-09-multi-tenant-saas-implementation.md` |
| Migration 028 | `migrations/versions/028_per_org_integrations.py` |
| ORM models (SlackInstallation, GitHubInstallation, WebhookSecret) | `shared/models.py` |
| pgcrypto encrypt/decrypt helper | `shared/installation_crypto.py` |
| Phase-3 config fields | `shared/config.py` (search `slack_client_id`) |
| Slack installation store | `integrations/slack/installation_store.py` |
| Slack OAuth endpoints | `integrations/slack/oauth.py` |
| Admin-only dep | `orchestrator/auth.py::current_org_id_admin_dep` |
| GitHub OAuth endpoints | `integrations/github/oauth.py` |
| GitHub auth resolution (per-org) | `shared/github_auth.py` |
| Slack multi-team runtime | `integrations/slack/main.py` |
| Slack DM notification fan-out | `integrations/slack/main.py::notification_loop` / `send_slack_dm` |
| Per-org webhook secret lookup | `orchestrator/webhooks/github.py::_secret_for_repo_full_name` |
| Installation-deleted handler | `orchestrator/webhooks/github.py` (search `installation`) |
| Settings UI hub | `web-next/app/(app)/settings/integrations/page.tsx` |
| Slack settings UI | `web-next/app/(app)/settings/integrations/slack/page.tsx` |
| GitHub settings UI | `web-next/app/(app)/settings/integrations/github/page.tsx` |
| Typed fetch helpers + hooks | `web-next/lib/integrations.ts`, `web-next/hooks/useIntegrations.ts` |
| Scoping coverage test | `tests/test_org_scoping_coverage.py` |
| Route mount in run.py | `run.py` (search `integrations/slack` and `integrations/github`) |
| Phase 2 handover | `docs/superpowers/plans/2026-05-11-handover-after-phase-2.md` (on main) |
| Phase 1 handover | `docs/superpowers/plans/2026-05-11-handover-after-phase-1.md` |

---

## Out of scope (intentionally deferred)

- **Linear per-org webhook secret** — same machinery as GitHub but lower event volume. Phase 3.5 or Phase 4.
- **Per-org Socket Mode app tokens** — slack-bolt's multi-team mode uses one app-level token; per-org socket tokens are not needed.
- **UI for setting per-org webhook secrets** — DB row can be inserted manually; no UI today.
- **testcontainers-backed real-DB isolation suite** — still recommended; carried forward from Phase 2 handover.
- **Per-org concurrency caps / quota enforcement** — Phase 4.
- **`agent/slack_assistant.py` system-token-per-org refactoring** — operational hardening; mitigated by E4's membership filter blocking cross-org user lookups.
- **Per-org Socket Mode** — not technically needed with HTTP mode and distributed app.

---

## Conversation summary

**Session arc (Phase 3):**

Phase 3 began by reading the Phase 2 handover and the Phase 3 spec (`2026-05-11-phase-3-per-org-integrations.md`). The user accepted the same risk as Phase 2 — skipping the testcontainers isolation suite — and asked for immediate implementation. A 31-task implementation plan was written covering 8 tracks (A through G plus tests).

Work was executed via subagent-driven development: each task had an implementer sub-agent, a spec-reviewer, and a code-reviewer. Two notable course-corrections arose during execution:

1. **C5 double-prefix bug**: slack-oauth routes were accidentally declared with `/api/integrations/slack/...` in the decorator, and the `run.py` mount added a second `/api` — effective path became `/api/api/...`. Caught by direct inspection during the scoping-coverage integration step. Fixed in commit `f874cb8` (strip `/api` from all route decorator paths). The same fix was applied proactively to the GitHub OAuth router before it was merged.

2. **E1 aiohttp stub**: tests for `PostgresInstallationStore` imported slack-bolt modules that transitively import `aiohttp`, which is not installed in the test venv. The solution was a focused `sys.modules` stub in the test file. This appeared alarming on first review but is correct — the actual runtime environment has aiohttp; only the test isolation layer lacks it.

**Final verification:** 808/808 tests passing (up from 762 at Phase 2 merge, +46 new). TypeScript `tsc` clean. Ruff at parity with main (207 vs 208 errors; pre-existing debt, no new issues introduced by Phase 3).

If anything in this document contradicts what you see in the code, **trust the code** and update this doc.
