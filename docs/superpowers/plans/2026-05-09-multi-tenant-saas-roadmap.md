# Auto-Agent → Multi-Tenant SaaS Roadmap

**Status:** draft
**Author:** session of 2026-05-09
**North star:** Anyone on the public internet can sign up, install auto-agent into their own Slack workspace + GitHub org + Claude account, and start running coding tasks on their own repos. Each customer's data, secrets, and resources are isolated. Billing reflects metered usage.

---

## How to read this plan

Six phases, each independently shippable. Each phase ends in a state that's strictly better than the one before — even if you stop midway, what you've shipped is useful. Phase numbers are sequencing, not strict gates: small bits of later phases can run in parallel where they don't depend on the spine.

Sizing is rough. "S" = ~1 week, "M" = ~2-4 weeks, "L" = ~1-2 months, of one focused engineer.

---

## Phase 0 — Lock down the present (S)

**Goal:** finish the in-flight work so the rest of the plan starts from a clean baseline. Most of this is already on `feat/multi-tenant-claude-credentials`.

**Already done on this branch:**
- Per-user Claude credential vaults (vault dir + auth probe + fallback resolution).
- Per-user Slack/Telegram routing; per-user auto-link by handle.
- LLM-driven Slack assistant (no slash commands).
- `shared/github_auth.py` seam — supports both PAT and GitHub App; all 19 call sites converted.
- Fan-out routing rule: notifications scoped to the task owner; admin only for system-scoped events.

**To finish before Phase 1:**
- Add `Co-Authored-By:` footer to every commit, derived from `task.created_by_user_id`. Keeps human attribution intact when running under the GitHub App.
- Surface a clean log line when `get_github_token()` falls back from App → PAT (silent fallback masks bugs).
- Delete `tests/test_claude_pairing.py` (stale from the PTY→OAuth rewrite) or rewrite it for the new shape.
- Merge the branch.

**Acceptance:**
- `git log` on a recent task PR shows `Co-Authored-By: <user> <email>` for the originating teammate.
- Logs explicitly say which auth mode is active.
- 711 tests still pass; no skipped pairing tests.

---

## Phase 1 — Self-serve user-level integrations (S–M)

**Goal:** an *individual* on your team (still single-tenant) can configure their own GitHub, Anthropic, and Claude credentials in the UI without an admin touching `.env`. This is the foundation for tenancy because it forces secrets out of env vars and into the database, encrypted.

**Work:**
1. **Encrypted secret store**: `pgcrypto` column type or AWS KMS via `boto3`. Pick one, write a tiny `shared/secrets.py` seam (`set(user_id, key, value)`, `get(user_id, key) → str | None`). All future per-user creds go through it.
2. **Per-user GitHub PAT**: `users.github_token_encrypted` (or via the secrets seam). `get_github_token(user_id=...)` resolves it; falls back to org/process-level if unset.
3. **Per-user Anthropic API key** (optional alternative to the per-user Bedrock config, but mostly future-proofs).
4. **Settings page**: under `/settings`, add three cards: "Connect Claude" (exists), "GitHub Personal Access Token", "Anthropic API key". Each shows current state + lets the user paste/clear.
5. **Public signup endpoint**: `POST /api/auth/signup` with email verification. Email goes via SMTP (cheap) or AWS SES. Today users are admin-created only.

**Acceptance:**
- A new teammate can `signup → verify email → log in → connect Claude → paste GitHub PAT → create a task on their repo` without an admin ever touching the VM.
- No env var change required to add a teammate.
- Removing `GITHUB_TOKEN` from `.env` doesn't break anyone who's pasted their own PAT.

**Why now:** every later phase depends on per-user/per-org credentials being a real concept, not a single env var. Doing this small first means later org work is just "add `org_id`" not "rewrite credential handling".

---

## Phase 2 — Org/tenant model (M)

**Goal:** every row that should belong to a customer belongs to a customer. Single-org installs (you, today) just have one org and nothing changes; multi-org becomes possible.

**Work:**
1. **Migration**: create `organizations(id, name, slug, created_at)`. Add `organization_id` (nullable initially) to `users`, `repos`, `tasks`, `suggestions`, `freeform_configs`, `task_history`, `task_messages`, `search_sessions`, `search_messages`, `scheduled_tasks`. Backfill: create one "default" org for the existing install; assign every existing row to it.
3. **Make `organization_id` NOT NULL** in a follow-up migration after backfill verified.
4. **Query scoping**: every read in `orchestrator/router.py` gets `WHERE organization_id = current_user.organization_id`. There are ~40 such sites; mechanical but error-prone — use a thin helper `scoped(query, user)` that injects the WHERE so future routes pick it up automatically.
5. **Many-to-many user↔org** (defer if YAGNI): start with one user → one org. Most early customers won't need multi-org-per-user.
6. **UI**: existing "/" header shows org name + switcher (no-op when single org). Org settings page (basic — name, slug, members).
7. **Repo onboarding**: `repos` becomes per-org; add a "Connect a repo" flow that lets the user pick from repos their (eventual) GitHub App install grants access to.

**Acceptance:**
- Same user logs in to two different orgs; sees different task lists, different repos, different suggestions.
- Cross-org access attempts (e.g. visiting another org's task ID via URL) return 404.
- The seed migration creates a sensible default org for existing data so production keeps working through the cutover.

**Why now:** this is the spine. Every later piece (per-org Slack, per-org billing, per-org workspace dirs) needs `organization_id` to scope to. Doing it later means a much bigger refactor.

**Risk:** the WHERE-scoping is the kind of thing where one missed query is a data leak. Pair with a pytest fixture that creates two orgs and asserts that org A's user can't see org B's data via *any* listed endpoint. Run as part of CI from this phase forward.

---

## Phase 3 — Per-org integrations via OAuth (M)

**Goal:** a customer signs up, clicks "Add to Slack" / "Install GitHub App", and auto-agent starts working in their workspace + on their repos. No manual env-var pasting.

**Work:**
1. **Slack OAuth install flow**:
   - Today: one `slack-bolt` `AsyncApp` per process, single bot token. Doesn't fit multi-tenant.
   - Switch to slack-bolt's *multi-team* mode: implement an `InstallationStore` backed by Postgres (`slack_installations(org_id, team_id, bot_token_encrypted, ...)`) and an OAuth callback at `/api/integrations/slack/oauth/callback`.
   - Bot tokens are now per-org. `send_slack_dm(slack_user_id, text, *, org_id)` looks up the right token.
   - Socket Mode is per-app; each customer install uses our *one* app, just installed in their workspace. Inbound events come tagged with `team_id` → resolve to org → dispatch.
2. **GitHub App** (per-org install):
   - Same App, customers install via the GitHub App page → callback → store `github_installations(org_id, installation_id)` per org.
   - `get_github_token(org_id=...)` mints from the org's installation.
3. **Telegram**: stays per-user (it already is). One bot for all orgs is fine — Telegram doesn't have the workspace-isolation model that Slack does.
4. **Linear, GitHub webhooks**: scope webhook signing secrets per-org. Today single global secret.

**Acceptance:**
- Two customers' Slack workspaces both work end-to-end without their data ever crossing.
- Removing the global `SLACK_BOT_TOKEN` env var doesn't break anything (each org's token is in DB).
- An org with no GitHub App installed gets a clear "Install the GitHub App" CTA, not a 500.

**Why now:** without this, you can't onboard a single external customer.

**Risk:** the slack-bolt single-app code path is *deeply* baked into our `inbound_loop` / `send_slack_dm`. Plan a clean rewrite of `integrations/slack/` rather than incremental — touching it incrementally will leave half the codebase assuming a global app instance.

---

## Phase 4 — Resource isolation + quotas (S–M)

**Goal:** one customer can't degrade another. Workspaces, queue, LLM spend all scoped + capped per org.

**Work:**
1. **Workspace dirs per org**: `/workspaces/<org_id>/task-<task_id>`. Avoids cross-org clones colliding on repo names. Cleanup script (`agent/lifecycle/cleanup.py`) prunes per-org.
2. **Queue caps per org**: today we have a global cap (5 concurrent) and per-repo cap (1). Add a per-org cap (default 5). Queue dispatcher rebalances across orgs fairly (round-robin or weighted by plan).
3. **LLM usage tracking**: every `BedrockProvider`/`AnthropicProvider` call records tokens in/out + cost estimate to `usage_events(org_id, kind, input_tokens, output_tokens, cost_cents, ...)`. Already most providers return usage info.
4. **Rate limits**: per-org task creation rate (e.g. 50/hour); per-org LLM token rate (e.g. 1M/day). Backed by Redis.
5. **Plan model**: `plans(name, max_concurrent_tasks, max_tokens_per_day, ...)`. Org has a `plan_id`. Limits read from the plan.

**Acceptance:**
- A loop in customer A spamming task creation does not delay customer B's queue.
- Disk usage breakdown by org is queryable.
- A customer hitting their plan limit gets a clear 429 with "upgrade" CTA, not a silent failure.

**Why now:** without this, one bad-actor customer or one runaway agent loop (we've seen these — task 164 looped for an hour) takes down everyone.

---

## Phase 5 — Billing + plans (M)

**Goal:** customers can pay; their plan determines limits.

**Work:**
1. **Stripe integration**:
   - Customer Portal for self-serve plan changes / payment method updates.
   - Subscription creation on signup or first paid action.
   - Webhooks to update `org.plan_id` + `org.billing_status` on `customer.subscription.*` events.
2. **Plan tiers**: pick three (free / pro / team is a fine start). Define limits per tier (concurrent tasks, monthly LLM token budget, member count).
3. **Metered overage** (defer to a follow-up): bill for tokens above a threshold. Phase 4's `usage_events` is the input.
4. **Trial → conversion flow**: 14-day free trial without payment method, then paywall.
5. **Invoices, receipts, dunning**: all via Stripe.

**Acceptance:**
- A new customer signs up, picks a plan, enters card, runs tasks; their token usage shows up in Stripe metered billing or as a fixed-price subscription.
- Failed payment → `org.billing_status = past_due` → tasks blocked with "fix billing" message; doesn't lose data.

**Why now:** monetisation gate. Don't pre-monetise — ship Phases 1-4 as a free closed alpha first, get product-market fit signal, *then* do this.

---

## Phase 6 — Operational hardening (M, ongoing)

**Goal:** running this in production without you babysitting it.

**Work:**
1. **OAuth login (Google + GitHub)**: replace username+password as the default. Use `authlib` or `python-social-auth`. Existing password users keep working as fallback.
2. **Per-org audit log**: every state-changing API call writes to `audit_events(org_id, user_id, action, target, ts)`. Surface in org settings.
3. **Per-org observability**: Grafana board with "tasks by status", "p95 task duration", "LLM spend / hour" filterable by org. Backed by Postgres → Prometheus exporter or a metrics columns in the existing tables.
4. **Customer data export**: `GET /api/orgs/{id}/export` returns a JSON dump of everything that org owns. GDPR Art. 15.
5. **Customer data deletion**: hard-delete the org and everything scoped to it on request, not just soft-delete. GDPR Art. 17.
6. **Status page**: public `status.auto-agent.dev` reflecting CI pass rate / agent dispatch latency.
7. **Backups**: nightly Postgres dump to S3 with 30-day retention. Test restore quarterly.
8. **SOC2-adjacent**: encryption at rest (RDS does this for free), encryption in transit (TLS everywhere), access logs, MFA for admin accounts.

**Acceptance:**
- A customer requests deletion → all rows scoped to their org are gone within 24h, verifiable.
- 30 days of agent task data survives a `terraform destroy && terraform apply` of the application tier.

**Why now:** required for any real B2B sale. Skip this phase and your first compliance-conscious customer asks "are you SOC2?" and you're stuck.

---

## What this plan deliberately does NOT include

These are real concerns but out of scope for the north star. List them so we're explicit:

- **Mobile apps**. Slack DMs already cover the on-the-go case (works on phones).
- **A custom IDE plugin**. The CLI + Web UI + Slack are enough.
- **On-premise / self-hosted enterprise edition**. Push back to "not yet" until at least 50 paying SaaS customers.
- **Multi-region failover**. Single AZ is fine until you have a paying customer who specifically asks for it.
- **A marketplace of skills / agents / templates**. The PO analyzer is enough for now.

---

## Sequencing summary (the only thing you'd put on a wall)

```
Phase 0  Lock down the present              ─┐ 1 week
Phase 1  Self-serve user integrations        │ 2 weeks
Phase 2  Org/tenant model                    │ 3-4 weeks
Phase 3  Per-org Slack + GitHub OAuth        │ 3-4 weeks
                                             │
        ────── First external alpha here ────┘
                                              ▶  ~ 2 months in

Phase 4  Resource isolation + quotas           2-3 weeks
Phase 5  Billing + plans                       2-4 weeks
                                                  
        ────── First paid customer here ──────
                                              ▶  ~ 4 months in

Phase 6  Operational hardening                 ongoing, ship in slices
```

**Realistic minimum to first paid customer: 4-5 months of one focused engineer.** Faster if scope is tightened (e.g. defer GDPR export, defer overage billing, accept "free closed alpha for 3 months before charging").

The largest single risk is **Phase 2 (org model)**: a missed `WHERE org_id = ?` is a data leak, and the WHERE clause discipline has to be in muscle memory before Phase 3 onboards real external data. Invest heavily in the cross-org pytest fixture from Phase 2 day one.
