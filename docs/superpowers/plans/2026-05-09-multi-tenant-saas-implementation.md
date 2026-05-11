# Multi-Tenant SaaS — Implementation Plan

**Companion to:** `2026-05-09-multi-tenant-saas-roadmap.md` (the strategic view).
**Status:** draft.
**Granularity:** medium — file-level for the load-bearing pieces, paragraph-level for the rest. Each phase is a separate PR (or series of PRs).

---

## Phase 0 — Lock down the present

**Already on `feat/multi-tenant-claude-credentials`; just close it out.**

### Work items

1. **`Co-Authored-By:` on commits** *(`agent/lifecycle/coding.py`, `agent/lifecycle/review.py`)*
   - When the coding agent runs, look up `task.created_by_user_id → User.username + display_name`.
   - Append `Co-Authored-By: Display Name <username@auto-agent.local>` to every commit message body.
   - Trailers must be at the *end* of the message, separated by a blank line — git's parser is strict.
   - Test: a synthetic task by user 2 produces a commit whose `git log --format=%(trailers)` includes the line.

2. **Auth-mode logging** *(`shared/github_auth.py`)*
   - On first call, log `github_auth_mode=app` or `github_auth_mode=pat`. On every fallback from app→pat, log a WARNING with the mint error.
   - Currently silent fallback masks bugs (we hit this on 2026-05-07 when the host PAT expired).

3. **Stale test cleanup** *(`tests/test_claude_pairing.py`)*
   - Replace the PTY-era tests with HTTP-flow tests of the new OAuth pairing module: PKCE generation, token-exchange success, token-exchange 4xx → PairingResult(False, ...).
   - Or just delete and rely on integration testing for now. Either is fine; don't leave 2 failing tests in the suite.

4. **Branch hygiene**
   - Squash the trailing debug-logging commits (`_pair_log` etc.) — they were diagnostic, keep the prod-relevant one.
   - PR description: link to ADR if you write one; otherwise list the four conventions stored in team-memory today (slack-interface / notification-routing / github-auth / analyzer-memory-writes).
   - Merge.

### Acceptance

- `git log` on a PR produced by user X shows X as Co-Authored-By.
- Logs explicitly state which GitHub auth mode is active and warn on fallback.
- 711+ tests pass; nothing skipped.
- Branch merged to `main`.

### Estimated: 3-5 days.

---

## Phase 1 — Self-serve user-level integrations

**Goal:** any teammate signs up + configures their own integrations from `/settings`. No env-var changes when adding teammates.

### Migration: 025_per_user_secrets

```sql
CREATE TABLE user_secrets (
    user_id      INT REFERENCES users(id) ON DELETE CASCADE,
    key          VARCHAR(64) NOT NULL,            -- e.g. "github_pat", "anthropic_api_key"
    value_enc    BYTEA NOT NULL,                  -- pgcrypto-encrypted
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);
CREATE INDEX ix_user_secrets_user ON user_secrets(user_id);

ALTER TABLE users ADD COLUMN email VARCHAR(255) UNIQUE;
ALTER TABLE users ADD COLUMN email_verified_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN signup_token VARCHAR(64);
```

Don't add new columns to `users` for each integration; use the secrets table. Cleaner future-proofing.

### New module: `shared/secrets.py`

```python
async def set(user_id: int, key: str, value: str) -> None
async def get(user_id: int, key: str) -> str | None
async def delete(user_id: int, key: str) -> None
async def list_keys(user_id: int) -> list[str]   # returns names only, never values
```

Implementation: `pgcrypto`'s `pgp_sym_encrypt` / `pgp_sym_decrypt` keyed off a single `SECRETS_PASSPHRASE` env var. Rotation later. Don't bring in KMS until the cost of doing so is less than the cost of a key compromise — for early SaaS that line is much further out than people think.

### Refactors

1. **`shared/github_auth.py::get_github_token(user_id: int | None = None)`**
   - Extends current signature. Resolution order:
     1. If `user_id`: look up `user_secrets[user_id, "github_pat"]` → use it.
     2. Else if GitHub App env vars set: mint installation token.
     3. Else `settings.github_token` (legacy PAT).
   - All ~19 call sites that already use the helper need a `user_id` thread-through. Most have `task.created_by_user_id` available; the system pollers (`ci_status_poller`, `pr_merge_poller`) keep the org/process-level path.

2. **`agent/llm/__init__.py::get_provider(...)` accepts `user_id`**
   - When set, `secrets.get(user_id, "anthropic_api_key")` overrides `settings.anthropic_api_key` for the AnthropicProvider path.
   - Bedrock path stays env-driven (it's infra-level).

### New endpoints (under `orchestrator/router.py`)

```
POST   /api/auth/signup              { email, password, display_name }  → 201, sends verify email
GET    /api/auth/verify/{token}                                          → 200, marks email verified
PATCH  /api/auth/me/email                                                → change email (re-verify)

GET    /api/me/secrets                                                   → ["github_pat","anthropic_api_key"]  (names only)
PUT    /api/me/secrets/{key}         { value: string | null }            → set or clear
DELETE /api/me/secrets/{key}                                             → clear
```

`PUT` returns 200 with no body (writes are silent on success). Frontend re-fetches list on save.

### UI work (`web-next/app/(app)/settings/`)

Three new sub-pages, each a thin component over `PUT /api/me/secrets/{key}`:

- `settings/github` — paste PAT, "Test connection" button (calls `gh user` via API).
- `settings/anthropic` — paste API key, test by counting tokens on a 10-token string.
- `settings/claude` — already exists.

Plus a public route:

- `app/(public)/signup/page.tsx` — email + password + display_name form. POSTs `/api/auth/signup`.
- `app/(public)/verify/[token]/page.tsx` — landing page after clicking the email link.

Email delivery: lightest path is AWS SES (since you're already on Azure/AWS for Bedrock). New env vars `SES_REGION`, `SES_FROM`. Plain-text emails for now — no fancy templates.

### Implementation order within the phase

1. Migration + `shared/secrets.py` + tests.
2. `shared/github_auth.py` per-user lookup + thread `user_id` through call sites.
3. New endpoints + signup flow without email yet (auto-verify in dev).
4. SES integration + email verification.
5. UI pages.

### Acceptance

- New teammate signup via UI → email arrives → click link → log in → paste GitHub PAT → create a task on their repo. No admin involvement.
- `GITHUB_TOKEN=` removed from VM `.env`; existing flows still work via per-user PAT.
- Cross-user secret reads return 401/404 (validated by a pytest test that mints two users and asserts user A can't read user B's secrets via any endpoint).

### Risks

- **`pgcrypto` rotation** — once you've encrypted with passphrase X, changing it means re-encrypting. Plan a `rotate_secrets` script for when you need it (don't build now).
- **Email deliverability** — SES sandbox mode caps you at 200 emails/day until you ask for prod access. Do that the day you start using SES.

### Estimated: 2-3 weeks.

---

## Phase 2 — Org/tenant model

**This phase is the spine. Take it slow. Pair the migration with the cross-org isolation test fixture before a single new endpoint ships.**

### Migration: 026_organizations + 027_scope_models

Two migrations, run in separate releases so we can verify backfill before flipping `NOT NULL`.

```sql
-- 026: create the table + add nullable org_id to every scope-able table
CREATE TABLE organizations (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(255) NOT NULL,
    slug        VARCHAR(64) NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Backfill default org from existing data
INSERT INTO organizations (name, slug) VALUES ('Default', 'default');

ALTER TABLE users               ADD COLUMN organization_id INT REFERENCES organizations(id);
ALTER TABLE repos               ADD COLUMN organization_id INT REFERENCES organizations(id);
ALTER TABLE tasks               ADD COLUMN organization_id INT REFERENCES organizations(id);
ALTER TABLE task_history        ADD COLUMN organization_id INT REFERENCES organizations(id);
ALTER TABLE task_messages       ADD COLUMN organization_id INT REFERENCES organizations(id);
ALTER TABLE suggestions         ADD COLUMN organization_id INT REFERENCES organizations(id);
ALTER TABLE freeform_configs    ADD COLUMN organization_id INT REFERENCES organizations(id);
ALTER TABLE search_sessions     ADD COLUMN organization_id INT REFERENCES organizations(id);
ALTER TABLE search_messages     ADD COLUMN organization_id INT REFERENCES organizations(id);
ALTER TABLE scheduled_tasks     ADD COLUMN organization_id INT REFERENCES organizations(id);
ALTER TABLE user_secrets        ADD COLUMN organization_id INT REFERENCES organizations(id);

-- Backfill: every existing row goes to the default org
UPDATE users            SET organization_id = (SELECT id FROM organizations WHERE slug='default');
UPDATE repos            SET organization_id = (SELECT id FROM organizations WHERE slug='default');
-- ... repeat for each scoped table
```

```sql
-- 027 (after backfill validated)
ALTER TABLE users            ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE repos            ALTER COLUMN organization_id SET NOT NULL;
-- ... etc.

-- Index every org_id column for query performance
CREATE INDEX ix_tasks_org_status        ON tasks(organization_id, status);
CREATE INDEX ix_repos_org               ON repos(organization_id);
CREATE INDEX ix_suggestions_org_status  ON suggestions(organization_id, status);
-- ... etc.
```

### New helper: `orchestrator/scoping.py`

```python
def current_org_id(payload: dict) -> int:
    """Return the org_id of the authenticated user. Raises 401 if missing."""

def scoped(query: Select, model: type, payload: dict) -> Select:
    """Inject WHERE model.organization_id = current_org_id(payload)."""
```

Every existing query in `orchestrator/router.py` that returns rows the user can access wraps with `scoped(...)`. Mechanical pass — the danger is missing one. Mitigation: a `pre-commit` lint that flags `select(Task | Repo | Suggestion | ...)` not followed by `scoped(...)` within ~20 lines.

### Cross-org isolation test fixture (write FIRST, before scoping any endpoint)

```python
# tests/conftest.py additions
@pytest.fixture
async def two_orgs():
    """Yield (org_a, user_a, repo_a, task_a, org_b, user_b, repo_b, task_b)
    so tests can assert user_a can never see B's data via ANY endpoint."""

# tests/test_org_isolation.py
@pytest.mark.parametrize("endpoint", [
    ("GET",   "/api/tasks"),
    ("GET",   "/api/tasks/{task_b.id}"),
    ("POST",  "/api/tasks/{task_b.id}/cancel"),
    ("GET",   "/api/repos"),
    ("GET",   "/api/repos/{repo_b.name}"),
    ("GET",   "/api/suggestions"),
    # ... every read + write endpoint
])
async def test_cannot_access_other_orgs_data(endpoint, two_orgs):
    """For every endpoint, user_a's session gets 404 (not 403; no info leak) on B's resources."""
```

This test is the deliverable for the phase. Without it, no PR after this phase merges. The discipline is: every new endpoint adds itself to the parametrize list.

### UI

- `web-next/components/org-switcher.tsx` — header dropdown (no-op when user is in one org).
- `web-next/app/(app)/settings/organization/page.tsx` — name, slug, members table, "Add member by email".
- New API: `POST /api/orgs/{id}/members`, `DELETE /api/orgs/{id}/members/{user_id}`.

### Many-to-many user↔org

**Defer for v1.** Use `users.organization_id` as a single FK. If/when needed, introduce `organization_memberships` table and migrate. Solo signups → solo orgs is fine for the first 100 customers.

### Acceptance

- Existing single-tenant install survives the migration with zero behavior change (everything in default org).
- Two test orgs cannot see each other's anything (validated by the parametrized test, currently ~30 endpoints).
- Org-switcher works in UI when a user has memberships in multiple orgs (manual seeding for now).

### Risks

- **Subtle WHERE leaks.** A future code reviewer ships `select(Task).where(Task.id == X)` without `.where(Task.organization_id == current)` and there's a leak. The lint + isolation fixture are non-negotiable.
- **Background workers.** Pollers (`ci_status_poller`, `pr_merge_poller`, `run_po_analysis_loop`) iterate over *all* tasks/configs. They don't get a user session. They need to scope by org explicitly when picking up work, especially if Phase 4 introduces per-org concurrency caps.
- **WebSocket subscriptions.** The `/ws` endpoint streams everything to whoever's connected. Add an `org_id` filter at delivery time, not just consumer-side.

### Estimated: 3-4 weeks.

---

## Phase 3 — Per-org Slack + GitHub OAuth

**Throw away today's `integrations/slack/main.py` single-app assumptions and rewrite for multi-installation.** Doing this incrementally will leave half the codebase carrying a global app instance.

### Migration: 028_per_org_integrations

```sql
CREATE TABLE slack_installations (
    org_id              INT PRIMARY KEY REFERENCES organizations(id) ON DELETE CASCADE,
    team_id             VARCHAR(32) NOT NULL UNIQUE,    -- Slack workspace ID (T...)
    bot_token_enc       BYTEA NOT NULL,
    bot_user_id         VARCHAR(32) NOT NULL,
    app_token_enc       BYTEA,                          -- xapp- for socket mode (optional)
    installed_by        VARCHAR(32),                    -- Slack user_id of installer
    installed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE github_installations (
    org_id              INT PRIMARY KEY REFERENCES organizations(id) ON DELETE CASCADE,
    installation_id     BIGINT NOT NULL UNIQUE,
    account_login       VARCHAR(128) NOT NULL,          -- the github org/user that installed
    installed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE webhook_secrets (
    org_id              INT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    source              VARCHAR(32) NOT NULL,           -- "github", "linear"
    secret_enc          BYTEA NOT NULL,
    PRIMARY KEY (org_id, source)
);
```

### Slack rewrite

#### Architecture change

- One Slack app, but installed in many workspaces. We're a "distributed" app on Slack's side.
- slack-bolt has multi-team support: `AsyncApp(installation_store=...)` instead of `AsyncApp(token=...)`.
- We implement `AsyncInstallationStore` backed by `slack_installations` table.
- Socket Mode: each team needs its own websocket. slack-bolt manages this when given a multi-team app.
- Inbound events arrive tagged with `team_id` → resolve to `org_id` → dispatch to per-org logic.

#### File map

- `integrations/slack/installation_store.py` (NEW) — Postgres-backed `AsyncInstallationStore`.
- `integrations/slack/oauth.py` (NEW) — handles `GET /api/integrations/slack/install` redirect to Slack OAuth, `GET /api/integrations/slack/oauth/callback` → store install, redirect to `/settings/integrations/slack`.
- `integrations/slack/main.py` — significant rewrite:
  - `_get_app()` returns a multi-team app singleton.
  - `send_slack_dm(slack_user_id, text, *, org_id, task_id=None)` — looks up `slack_installations[org_id]` for the right bot token, then posts.
  - `_handle_dm_event` already takes `event` from a tagged team — resolve `org_id` via `slack_installations.team_id`, then run the existing per-org logic.

#### New API

```
GET  /api/integrations/slack/install            (org admin)  → 302 to Slack OAuth
GET  /api/integrations/slack/oauth/callback                  → consumes ?code, stores install, redirects
POST /api/integrations/slack/uninstall          (org admin)  → revokes, deletes row
GET  /api/integrations/slack                    (org admin)  → returns install state
```

#### Settings UI

- `web-next/app/(app)/settings/integrations/slack/page.tsx` — "Add to Slack" button (linking to `/api/integrations/slack/install`), or "Connected to <workspace>, disconnect" if already installed.

### GitHub App rewrite

Easier than Slack — the GitHub App OAuth flow is just "user clicks Install on github.com, we get a callback".

#### File map

- `integrations/github/oauth.py` (NEW) — handles `GET /api/integrations/github/install` → redirect to GitHub App install URL; `GET /api/integrations/github/oauth/callback` → consume `?installation_id`, store in `github_installations`.
- `shared/github_auth.py::get_github_token(*, org_id, user_id=None)` — resolution order changes:
  1. If `user_id` set + that user has a per-user PAT in `user_secrets` → use it.
  2. Else if `org_id` has a `github_installations` row → mint installation token.
  3. Else fall back to env-var `GITHUB_APP_*` (legacy single-tenant).
  4. Else fall back to env-var `GITHUB_TOKEN` (legacy PAT).

#### Settings UI

- `web-next/app/(app)/settings/integrations/github/page.tsx` — "Install GitHub App" button (deep links to `https://github.com/apps/auto-agent/installations/new?state=<org_id>`), or "Installed on <github org>".

### Webhook scoping

Today GitHub webhooks (`/api/webhooks/github`) verify against a single `GITHUB_WEBHOOK_SECRET`. Per-org: the install creates a webhook on the customer's repos, signed with a per-org secret. Webhook handler reads `X-Hub-Signature-256`, looks up the org by repo name → `webhook_secrets`, verifies.

Linear webhooks: same shape.

### Telegram

Stays as-is. Telegram doesn't have per-workspace isolation — one bot covers all users via per-user `chat_id` linking. No per-org concept needed.

### Acceptance

- Two customer orgs, two Slack workspaces, two GitHub orgs. Tasks created in workspace A produce notifications only in workspace A's bot DM; PRs land on org A's repos. No cross-pollination.
- Removing global `SLACK_BOT_TOKEN` / `GITHUB_TOKEN` env vars from VM `.env` doesn't break either org.
- An org without Slack installed shows "Install Slack" CTA, not a 500.

### Risks

- **slack-bolt multi-team is less battle-tested than single-app.** Plan an integration test that mounts two fake `team_id`s and asserts events route to the right org. The `MockSocketModeClient` in slack-bolt makes this feasible.
- **Customer rotates / uninstalls the GitHub App.** Handle the `installation.deleted` webhook → mark `github_installations` deleted → block tasks until reinstall.

### Estimated: 3-4 weeks.

---

## Phase 4 — Resource isolation + quotas

**Goal:** one customer's runaway loop doesn't degrade another. Track usage. Enforce plan limits.

### Migration: 029_quotas

```sql
CREATE TABLE plans (
    id                          SERIAL PRIMARY KEY,
    name                        VARCHAR(64) NOT NULL UNIQUE,   -- "free", "pro", "team"
    max_concurrent_tasks        INT NOT NULL,
    max_tasks_per_day           INT NOT NULL,
    max_input_tokens_per_day    BIGINT NOT NULL,
    max_output_tokens_per_day   BIGINT NOT NULL,
    max_members                 INT NOT NULL,
    monthly_price_cents         INT NOT NULL DEFAULT 0
);

INSERT INTO plans (name, max_concurrent_tasks, max_tasks_per_day, max_input_tokens_per_day, max_output_tokens_per_day, max_members)
VALUES
  ('free',  1, 5,    1_000_000,    250_000,   3),
  ('pro',   3, 50,  10_000_000,   2_500_000,  5),
  ('team',  5, 200, 50_000_000,  12_500_000, 25);

ALTER TABLE organizations ADD COLUMN plan_id INT REFERENCES plans(id);
UPDATE organizations SET plan_id = (SELECT id FROM plans WHERE name='free');
ALTER TABLE organizations ALTER COLUMN plan_id SET NOT NULL;

CREATE TABLE usage_events (
    id              BIGSERIAL PRIMARY KEY,
    org_id          INT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    task_id         INT REFERENCES tasks(id) ON DELETE SET NULL,
    kind            VARCHAR(32) NOT NULL,         -- "llm_call", "task_dispatch", "workspace_clone"
    model           VARCHAR(64),                  -- for llm_call
    input_tokens    INT NOT NULL DEFAULT 0,
    output_tokens   INT NOT NULL DEFAULT 0,
    cost_cents      NUMERIC(10,4) NOT NULL DEFAULT 0,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_usage_events_org_time ON usage_events(org_id, occurred_at DESC);
```

### Workspace dirs

`agent/workspace.py::clone_repo` changes the directory name:

```python
# was: /workspaces/task-{task_id}
# now: /workspaces/{org_id}/task-{task_id}
```

Cleanup (`agent/lifecycle/cleanup.py`) prunes per-org dirs.

### Queue

`orchestrator/queue.py`:

- Today: global cap (5 concurrent) + per-repo cap (1 active per repo).
- Add: per-org cap from `org.plan.max_concurrent_tasks`. Round-robin dispatch across orgs so one org with 100 queued tasks doesn't starve another with 1.
- Per-org daily counter (Redis sorted set keyed by date) enforces `max_tasks_per_day`.

### Usage tracking

Hook into the LLM provider boundary:

- `agent/llm/bedrock.py`, `agent/llm/anthropic.py`: every `complete()` call returns a `TokenUsage` already; emit a `usage_events` row per call (background — don't block the response).
- Cost estimate from a static price-per-million-tokens table per model.

### Rate limiter middleware

- `orchestrator/router.py` middleware on task-create endpoints checks daily counter + plan limit. Returns 429 with retry-after.
- LLM token rate: enforced in the provider wrapper. When budget is exhausted, the task transitions to a new state `BLOCKED_ON_QUOTA` with a clear message (mirror of `BLOCKED_ON_AUTH`).

### Settings UI

- `settings/usage` page: this month's tokens used, tasks dispatched, % of plan, simple chart.
- Plan card in org settings shows current plan + "Upgrade" CTA (no-op until Phase 5).

### Acceptance

- Loop test: org A spams 100 task-create calls. After their plan limit (5/day on free), 95 are 429'd with "limit reached, upgrade or wait until midnight". Org B is unaffected.
- Disk usage breakdown by org is queryable: `du -sh /workspaces/*` shows each org's footprint.
- A task that hits the LLM budget transitions to `BLOCKED_ON_QUOTA`; the user sees a clear DM/banner.

### Risks

- **Round-robin queue dispatch is genuinely tricky** when plans differ. Start with simple round-robin; weighted (Pro orgs get 3:1 priority over Free) is a future tweak, not a v1 requirement.
- **LLM cost calculation drifts as Anthropic changes prices.** Centralise in `shared/pricing.py`. Update when pricing changes.

### Estimated: 2-3 weeks.

---

## Phase 5 — Billing + plans

**Goal:** customers can pay; their plan determines what they can do; failed payment gracefully blocks new work without losing data.

### Stripe primitives

- One Stripe Product per plan tier (free, pro, team). Free is a Stripe product with $0 price, makes the data model uniform.
- Each org gets a `stripe_customer_id` on creation.
- Subscriptions are 1:1 with plans for v1 (no metered overage in v1 — defer).

### Migration: 030_billing

```sql
ALTER TABLE organizations ADD COLUMN stripe_customer_id VARCHAR(64) UNIQUE;
ALTER TABLE organizations ADD COLUMN stripe_subscription_id VARCHAR(64);
ALTER TABLE organizations ADD COLUMN billing_status VARCHAR(32) NOT NULL DEFAULT 'active';
                                          -- "active" | "trialing" | "past_due" | "canceled"
ALTER TABLE organizations ADD COLUMN trial_ends_at TIMESTAMPTZ;
```

### Endpoints

```
POST /api/billing/portal            → redirects to Stripe Customer Portal
POST /api/billing/checkout          { plan_id }   → Stripe Checkout session URL

POST /api/webhooks/stripe                          → Stripe webhook handler
```

### Stripe webhook handler

Listens for:

- `customer.subscription.created` / `customer.subscription.updated` → set `org.plan_id`, `billing_status`, `stripe_subscription_id`.
- `customer.subscription.deleted` → downgrade to free.
- `invoice.payment_failed` → `billing_status = past_due`.
- `invoice.payment_succeeded` → `billing_status = active`.

### Plan-gate middleware

Extends Phase 4's rate limiter:

- If `org.billing_status == 'past_due'` for >7 days, block task creation with "fix billing" CTA.
- Soft-cap (free trial expired → downgrade to free, not block).

### Onboarding flow

- Signup → org created with 14-day trial on Pro tier (no payment method).
- Day 12: email reminder.
- Day 14+1: downgrade to Free, banner in UI ("trial ended, upgrade").

### UI

- `settings/billing` — current plan, "Manage subscription" → Stripe Customer Portal, current usage vs plan, next invoice date.
- Plan tier cards on signup + during trial.

### Acceptance

- Card-on-file customer subscribes via Stripe Checkout → webhook fires → `org.plan_id = pro`. Their plan limits (concurrent tasks, daily tokens) reflect the new tier within 60 seconds.
- Failed payment triggers a 7-day grace period banner. Day 8 blocks task creation with "fix billing".
- Cancel subscription via Stripe Customer Portal → at period end, downgraded to Free without data loss.

### Risks

- **Stripe webhooks can fire out of order or double.** Idempotency: use the Stripe event ID as a primary key in a `stripe_events_processed` table; skip duplicates.
- **The first failed charge is usually a card-expired teenager moment.** Make the "fix billing" CTA front-and-center; don't hide it.

### Estimated: 2-4 weeks (depending on how fancy the trial flow + dunning gets).

---

## Phase 6 — Operational hardening

**Goal:** an external customer's "is this trustworthy?" question has good answers.

This phase is *ongoing* — ship slices throughout the rest of the lifecycle, don't wait until end. Below is the punch list, not a single PR.

### OAuth login (1-2 weeks)

- `authlib` + Google + GitHub providers.
- Existing username/password rows keep working; new signups get an `auth_provider` column.
- Linkage: a user can connect both providers (so they can log in via either).
- Files: new `orchestrator/auth_oauth.py`, callback handlers under `/api/auth/oauth/{provider}/...`.

### Per-org audit log (3-5 days)

- `audit_events(id, org_id, user_id, action, target_type, target_id, payload, ip, user_agent, ts)` table.
- Middleware in `orchestrator/router.py` writes a row for every state-changing call.
- UI: `settings/audit-log` page, paginated.

### Per-org observability (1-2 weeks)

- Push to Prometheus or OTLP. Per-org tags on every metric (`auto_agent_task_status_total{org="..."}`).
- Grafana boards: tasks, LLM spend, queue depth, dispatch latency. Filter by `org`.
- Alert on per-org anomalies (5-sigma jump in task creation rate, e.g. abuse).

### Customer data export (3-5 days)

- `GET /api/orgs/{id}/export` (org admin only) → returns a tar.gz of:
  - `tasks.json` — every task they own
  - `messages.json`, `history.json`
  - `repos.json`
  - `secrets-redacted.json` — names only, never values

### Customer data deletion (3-5 days)

- `POST /api/orgs/{id}/delete` (org admin, requires re-auth) → background job that hard-deletes:
  - All scoped rows (tasks, messages, history, suggestions, freeform_configs, search_*, scheduled_tasks).
  - The org's secrets (encrypted columns + their FKs).
  - The Stripe customer (if billing active, customer.delete via Stripe API).
  - Slack/GitHub installations (revoke tokens).
- Soft-grace: 30-day "are you sure" period before hard delete.

### Backups (1-2 days + restore drill)

- `pg_dump` to S3 nightly (encrypted with KMS).
- 30-day retention.
- Quarterly restore drill — pull a backup into a staging DB and verify it's complete.

### Status page (1-2 days)

- Self-hosted via `cachet` or `kuma`, OR pay for `instatus`/`statuspage` ($20/mo).
- Probes: API health, agent dispatch latency, Slack delivery latency.

### SOC2-adjacent (ongoing)

- Encryption at rest: RDS does this for free (verify it's on).
- Encryption in transit: ensure Caddy/whatever-fronts-FastAPI does TLS termination, internal http only on localhost.
- Access logs: web-server + `audit_events` cover this.
- MFA for admin: Google Workspace + force MFA, since admins log in via Google OAuth in this phase.

### Acceptance

- A customer requests deletion → 30-day window passes → all rows scoped to their org are gone within 24h of the trigger; verify via direct SQL.
- 30 days of data survives a `terraform destroy && terraform apply`.
- A test customer can pay, run tasks, dispute a charge in Stripe, request data export + deletion.

### Risks

- **GDPR scope creep** — your customers' employees' personal data lives in their tasks. Strictly scope deletion to org-owned data; don't try to be GDPR for data you don't control.
- **Audit-log volume** — every state-changing call. Could be hundreds of MB/month for an active customer. Plan a 90-day retention + cold-archive policy from day one.

### Estimated: 1-2 months elapsed, but spread across other phases.

---

## Cross-phase concerns

### Single source of truth for "which mode am I in"

Add to `shared/config.py`:

```python
@property
def deployment_mode(self) -> Literal["single-tenant", "multi-tenant"]:
    """Single-tenant if no organizations table or 1 org. Multi-tenant otherwise."""
```

Lets feature flags branch on this without duplicate "is this a SaaS install?" logic.

### Migrations in production

- All migrations must be backward-compatible with the previous *running* code. New columns nullable until backfilled. Drop columns in a separate migration after a release window.
- Use Postgres advisory locks in `alembic upgrade head` so two pods don't race during rolling deploys.

### Test fixtures

- A `multi_tenant_db` pytest fixture used by every integration test from Phase 2 onwards. Seeds two orgs, one user each, one repo each. Asserting cross-org isolation becomes a one-liner.
- A `billing_stub` fixture mocks Stripe webhooks for Phase 5.

### Documentation

- `docs/saas/` directory with one markdown per integration: how to install Slack, GitHub App, Anthropic, billing flow.
- Public-facing docs site (mkdocs or similar) — defer until Phase 5 launches.

### Commit hygiene

- Each migration is its own commit. No "phase 2 commit" that includes 12 schema changes.
- Each phase merges as one or more reviewable PRs (target ~500 lines each); the org-scoping pass is most usefully shipped one model at a time.

---

## Definition of done — north star

A new customer can:

1. Visit a public landing page, click "Sign up".
2. Enter email + password (or sign in with Google/GitHub).
3. Verify email, log in.
4. Be guided through:
   - "Install on Slack" → Slack OAuth → DM the bot from their workspace.
   - "Install GitHub App" → pick repos.
   - "Connect Claude" → existing OAuth flow.
5. Create their first task by DM'ing the bot.
6. See the task progress through the existing pipeline — same UX you use today, just scoped to their data.
7. Get billed appropriately when they hit the free tier's limits.
8. Export or delete their data on demand.

When that flow works without you ever logging into the VM, you've hit the north star.
