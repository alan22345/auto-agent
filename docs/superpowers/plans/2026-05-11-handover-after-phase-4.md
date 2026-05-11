# Handover — multi-tenant SaaS, after Phase 4

**Date:** 2026-05-11
**Branch:** `feat/phase-4-quotas` — branched off `feat/phase-3-per-org-integrations` (PR #40, not yet merged)
**Phase 3 status:** Still on PR #40 — not yet merged to `main`. Phase 4 builds on top.
**Baseline:** 808 tests at Phase 3 merge target. Phase 4 adds 37 → **845 tests, 0 failures**.

---

## What's done

### Track A — Schema & ORM (migration 029)

- **Migration 029** (`migrations/versions/029_plans_and_usage.py`):
  - `plans` table (id, name unique, `max_concurrent_tasks`, `max_tasks_per_day`, `max_input_tokens_per_day`, `max_output_tokens_per_day`, `max_members`, `monthly_price_cents`).
  - Seeded three rows: `free (1, 5, 1M, 250k, 3, 0)`, `pro (3, 50, 10M, 2.5M, 5, 0)`, `team (5, 200, 50M, 12.5M, 25, 0)`.
  - `organizations.plan_id` (NOT NULL FK after backfill to `free`).
  - `usage_events` table — id (BigInt), org_id (CASCADE), task_id (SET NULL, nullable), kind (str), model (str, nullable), input_tokens, output_tokens, cost_cents (Numeric(10,4)), occurred_at (now() default). Index on `(org_id, occurred_at DESC)`.
  - Postgres enum extension: `ALTER TYPE taskstatus ADD VALUE 'BLOCKED_ON_QUOTA'` (uppercase, matching SQLAlchemy's NAME-serialization) AND `'blocked_on_quota'` (lowercase, mirroring the 003/022 pattern). See "Critical things to know" for the historic enum-casing inconsistency.
- **Three ORM additions to `shared/models.py`**: `Plan`, `UsageEvent`, plus `plan_id` column + `plan = relationship("Plan", lazy="joined")` on `Organization`. `TaskStatus.BLOCKED_ON_QUOTA = "blocked_on_quota"` enum value.
- **`orchestrator/state_machine.py`**: BLOCKED_ON_QUOTA is reachable from QUEUED/PLANNING/CODING and exits to QUEUED/PLANNING/CODING/FAILED. BLOCKED_ON_AUTH's exits were also added explicitly (previously implicit).
- **Signup wired with plan_id**: `orchestrator/router.py::create_user` resolves the free plan and assigns `plan_id=free_plan.id` so new orgs satisfy the NOT NULL constraint.

### Track B — Pricing

- **`shared/pricing.py`**: `PRICE_PER_MILLION_TOKENS` table (claude-sonnet/opus/haiku 4.5/4.6 + 4.0 baselines) and `DEFAULT_PRICE_CENTS_PER_MILLION` fail-safe overestimate. `estimate_cost_cents(model, in, out) -> Decimal` returns fractional cents for `usage_events.cost_cents`.

### Track C — Quota helpers + usage emission

- **`shared/quotas.py`**: pure read helpers over an `AsyncSession`:
  - `QuotaExceeded(Exception)` — canonical here, imported by `agent/loop.py` and `orchestrator/router.py`.
  - `get_plan_for_org`, `count_active_tasks_for_org`, `count_tasks_created_today`, `sum_tokens_today`, `would_exceed_token_cap`, `enforce_task_create_limit`.
  - `_utc_day_bounds` returns UTC `[00:00:00, 23:59:59.999999]` — switch to per-org TZ later if a customer asks.
  - `_ACTIVE_STATUSES` is a local mirror of `orchestrator.queue.ACTIVE_STATUSES` (avoid cross-layer import — update both together).
- **`shared/usage.py::emit_usage_event`**: append-only writer for `usage_events`. Best-effort — logs and swallows DB errors so a quota-accounting failure never crashes a paying customer's task. Originally placed in `orchestrator/usage.py` but moved to `shared/` to keep `agent/loop.py` within its allowed import layer.
- **`agent/loop.py::UsageSink`** (dataclass): `emit(model, usage)` writes one row via `shared.usage`; `would_exceed_token_cap(est_input, est_output)` looks up the plan and today's usage. Optional `db_session` parameter passes the caller's session to keep tests transactional.
- **Pre-call gate in `_run_agentic`**: before every `provider.complete()`, estimate input tokens as `sum(len(m.content))/4` and check `would_exceed_token_cap` against `max_tokens=8192` for output. Raises `QuotaExceeded` if it would exceed. Wrapped with `try/except LookupError: raise QuotaExceeded(...)` so a no-plan misconfig surfaces clearly.
- **Post-call emit** added to both `_run_agentic` and `_run_passthrough`. Passthrough mode skips the pre-call gate (no cheap token estimate available).
- **`agent/lifecycle/factory.py::create_agent`** now accepts `org_id` and constructs the UsageSink. Threaded through `coding.py`, `planning.py`, `review.py`, `conversation.py` lifecycle handlers (11+ call sites). The eval-only `eval/providers/agent_provider.py` correctly passes `org_id=None` and runs without quota gating.
- **Subagent inheritance**: `agent/tools/base.py::ToolContext` gained `usage_sink` field; `agent/tools/subagent.py` passes it into the nested `AgentLoop`.

### Track D — Per-org workspace dirs

- **`agent/workspace.py::_workspace_path`** helper: `<WORKSPACES_DIR>/<org_id>/task-<task_id>` when `organization_id` is set; legacy `<WORKSPACES_DIR>/task-<task_id>` when None (back-compat).
- `clone_repo` and `cleanup_workspace` both accept `organization_id: int | None`. Lifecycle handlers (coding/planning/review/conversation/cleanup, and `agent/conflict_resolver.py`) thread `task.organization_id` through.
- System-level callers without task context (`po_analyzer`, `harness`, `architect_analyzer`) intentionally use the legacy path.

### Track E — Per-org queue cap

- **`orchestrator/queue.py::_org_at_concurrency_cap(session, org_id) -> bool`**: looks up `plan.max_concurrent_tasks` via `shared.quotas` and compares with `count_active_tasks_for_org`. `LookupError` (no plan) is treated as not-capped (defensive).
- `next_eligible_task` memoizes capped orgs per dispatcher tick so a single org with 100 queued tasks doesn't trigger 100 plan lookups.
- `can_start_task` also consults the per-org cap.

### Track F — Rate-limit task creation

- **`shared/quotas.py::enforce_task_create_limit(session, org_id)`** raises `QuotaExceeded` when `count_tasks_created_today >= plan.max_tasks_per_day`.
- **`orchestrator/router.py::create_task`** now calls `quotas.enforce_task_create_limit` right after `caller_org_id` is resolved (from JWT cookie or `current_org_id_dep` — keeps both paths so webhook callers still work). On exception, returns HTTP 429 with `Retry-After` set to seconds-until-UTC-midnight via `_seconds_until_utc_midnight()`.

### Track G — BLOCKED_ON_QUOTA on token-cap hit

- **Lifecycle handlers** (`coding.py`, `planning.py`, `review.py`, `conversation.py`) wrap `await agent.run(...)` with `except QuotaExceeded:` that transitions the task to `BLOCKED_ON_QUOTA` and returns cleanly (no FAILED). The QuotaExceeded handler is ALWAYS placed BEFORE the existing `except Exception:` blocks.
- **`orchestrator/unblock.py::unblock_quota_paused(session)`** sweeps all `BLOCKED_ON_QUOTA` tasks; for each org whose today's tokens are now under cap, transitions back to QUEUED. Returns the count moved. Commit is the caller's responsibility (preserves test fixture rollback).
- **Wired into `run.py`**: `unblock_quota_paused` runs before each `_try_start_queued` call (4 sites). At UTC midnight, `sum_tokens_today` resets to 0 and every paused task is promoted in the next tick.

### Track H — Settings UI: `/settings/usage`

- **`GET /api/usage/summary`** in `orchestrator/router.py` returns `UsageSummary` (Pydantic in `shared/types.py`) — `plan: PlanRead` + active_tasks + tasks_today + input_tokens_today + output_tokens_today. Scoped via `current_org_id_dep`.
- **`web-next/lib/usage.ts`**: typed `fetchUsageSummary()`.
- **`web-next/hooks/useUsage.ts::useUsageSummary`**: TanStack Query, 60s refetch.
- **`web-next/app/(app)/settings/usage/page.tsx`**: plan card + four progress bars. Upgrade button is disabled (Phase 5).
- **Settings sidebar**: added "Usage" between Organization and Integrations in `web-next/app/(app)/settings/layout.tsx`.

### Tests — +37 new, 845/845 passing

| File | Count | What it covers |
|---|---|---|
| `tests/test_models_phase4.py` | 4 | ORM introspection (Plan, UsageEvent, Organization.plan_id, BLOCKED_ON_QUOTA enum) |
| `tests/test_state_machine.py` | 3 | TRANSITIONS dict — BLOCKED_ON_QUOTA entry/exit + BLOCKED_ON_AUTH exit |
| `tests/test_migration_029.py` | 2 | Alembic upgrade-029 round-trip (skip-if no DATABASE_URL) |
| `tests/test_pricing.py` | 3 | Known model cost; unknown model fallback; zero tokens |
| `tests/test_quotas.py` | 6 | Each pure helper in shared/quotas.py (real DB; per-test rolled-back session) |
| `tests/test_usage_event_emission.py` | 1 | AgentLoop emits one usage_events row per provider.complete() |
| `tests/test_workspace_per_org_dirs.py` | 4 | `_workspace_path` shape; cleanup only removes per-org subtree |
| `tests/test_queue_per_org_cap.py` | 2 | Org A at cap → its task skipped; can_start_task blocks |
| `tests/test_rate_limit_task_create.py` | 1 | Third POST after 2-tasks-per-day cap returns 429 with Retry-After |
| `tests/test_blocked_on_quota.py` | 8 | Quota errors transition to BLOCKED_ON_QUOTA in each lifecycle handler |
| `tests/test_blocked_on_quota_unblock.py` | 2 | Under-cap sweep promotes back to QUEUED; empty case returns 0 |
| `tests/test_usage_endpoint.py` | 1 | `GET /api/usage/summary` returns plan + totals |

(The `test_blocked_on_quota.py` count of 8 includes module-import checks added by the implementer.)

---

## Production state

**NOT YET DEPLOYED.** Phase 4 has not been pushed. Phase 3 (PR #40) also hasn't been merged to main yet — confirm with the user before stacking deploys.

**Env vars required before deploy:** NONE. Phase 4 introduces no new env vars.

**Alembic:**
```
docker compose exec auto-agent alembic upgrade head
```
Verify `alembic current` shows `029`.

**Smoke tests post-deploy:**

1. **Per-org rate limit**: log in as a fresh org on the free tier; create 5 tasks in succession; the 6th should return HTTP 429 with `Retry-After` ~= seconds until UTC midnight.
2. **Per-org concurrency cap**: with two orgs each on free, start one task in org A (it'll be CODING). Queue a second task in org A → it stays QUEUED. Queue a task in org B → it starts. (Smoke against the dispatcher; needs a live agent.)
3. **Usage page**: navigate to `/settings/usage`; confirm the four bars render with live values from `/api/usage/summary`.
4. **Token-cap exhaustion + unblock**: contrived — set a temporary low `max_input_tokens_per_day` on the test org's plan, run an agent task until it raises QuotaExceeded, confirm task state becomes BLOCKED_ON_QUOTA. (UTC midnight will auto-unblock; or manually update the plan cap and the next tick of the unblock sweep will promote it.)

---

## What's NOT done (priority order)

1. **PR review + merge of #40 (Phase 3)** — still open. Phase 4 sits on top of it. Either land #40 first, then open the Phase 4 PR onto main; or open #41 (Phase 4) targeting #40's branch.
2. **Live VM deploy** — alembic upgrade head; smoke tests above.
3. **Phase 2 deploy verification** — still pending (carried forward from Phase 2 → 3 handovers).
4. **Per-org notifications when at 80%/100% of cap** — UX nicety; deferred.
5. **Weighted round-robin queue dispatch** — Pro orgs 3:1 over Free; spec says "start simple". Add only when free-org floods become an observed problem.

---

## Critical things to know

### `taskstatus` Postgres enum: historic uppercase/lowercase inconsistency

Migration 001 created the `taskstatus` Postgres enum with UPPERCASE values (`INTAKE`, `CLASSIFYING`, ..., `BLOCKED`, `FAILED`). Migrations 003 and 022 added LOWERCASE values (`'awaiting_clarification'`, `'blocked_on_auth'`) via `ALTER TYPE`. SQLAlchemy's `Enum(TaskStatus)` column type serializes Python enum members by NAME (uppercase), so those lowercase variants are essentially dead values — the ORM never queries them.

Migration 029 follows the 022 pattern AND adds the uppercase variant (`'BLOCKED_ON_QUOTA'`) that the ORM actually uses. The `'blocked_on_quota'` lowercase variant is kept for consistency with the historical pattern.

**Implication for the dev DB:** if your local Postgres was bootstrapped through migrations 001 → 028, then migration 003's `'awaiting_clarification'` is lowercase only, missing the uppercase variant. ORM queries that filter `status IN (TaskStatus.AWAITING_CLARIFICATION, ...)` fail with `invalid input value for enum taskstatus`. This was hidden until Phase 4 added real-DB tests. A one-off fix during Phase 4 manually added the uppercase variants to the dev DB:

```sql
ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'AWAITING_CLARIFICATION';
ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'BLOCKED_ON_AUTH';
ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'BLOCKED_ON_QUOTA';
```

A future cleanup task should add a migration that explicitly normalizes the enum.

### Test fixture: per-test AsyncEngine

`tests/conftest.py::session` creates a fresh `AsyncEngine` per test bound to the test's event loop, then wraps in a transaction that rolls back at end. This is required because pytest-asyncio's per-test event loops can't share connections via the global `shared.database.async_session` engine — asyncpg raises "cannot perform operation: another operation is in progress" on the second test that tries to reuse a connection from a different loop.

The fixture **skips** when `DATABASE_URL` is unset, so the mock-based portion of the suite still runs standalone. Phase 4 DB tests skip locally without `DATABASE_URL` but exercise fully in `docker compose exec auto-agent pytest` and in CI.

### `emit_usage_event` is best-effort

A DB write failure during usage accounting is logged (`usage_event_write_failed`) and swallowed — it must NEVER crash an in-flight task. Implication: budgets are "soft" — if a flood of `usage_events` write failures happens, an org could quietly exceed its daily cap. This is acceptable for v1 (operational visibility, not billing). Phase 5 will tighten this if real money depends on it.

### UTC day boundary, not org-local TZ

`sum_tokens_today` and `count_tasks_created_today` use `_utc_day_bounds`. A user in a non-UTC timezone hitting their quota at 6pm local will see the reset at midnight UTC, not midnight local. v1 keeps this predictable; per-org TZ can be added later.

### Migration 029 round-trip test mutates the dev DB

`tests/test_migration_029.py::test_downgrade_from_029_to_028_drops_plans_and_column` runs `alembic upgrade 029` → `alembic downgrade 028` → assertions → `alembic upgrade 029` (cleanup, restores state). Aware of this: if you `^C` the test mid-run, the DB may be left at 028. Recover with `alembic upgrade 029` manually.

### `would_exceed_token_cap` opens its own DB session

`UsageSink.would_exceed_token_cap` always opens a fresh `async_session()` rather than reusing the caller's session. This is intentional — the gate runs many times per agent turn and shouldn't conflict with the caller's transactional context. The pre-call gate IS the slowest part of the LLM-loop turn (one extra Postgres round-trip per call); if profiling shows it's a bottleneck, add a per-tick cache in `UsageSink`.

### `monthly_price_cents` is dead until Phase 5

`plans.monthly_price_cents` is in the schema and ORM but unused by Phase 4. Reading it in `PlanRead` returns 0 for all three seeded tiers. Phase 5 will populate real Stripe-driven values.

---

## Where to find things

| Topic | Path |
|---|---|
| Phase 4 implementation plan | `docs/superpowers/plans/2026-05-11-phase-4-resource-isolation-quotas.md` |
| Migration 029 | `migrations/versions/029_plans_and_usage.py` |
| Plan + UsageEvent ORM | `shared/models.py` (after Organization, after TaskMessage) |
| Pricing | `shared/pricing.py` |
| Quotas helpers | `shared/quotas.py` |
| Usage event emitter | `shared/usage.py` |
| UsageSink + agent gate | `agent/loop.py` (search `class UsageSink`) |
| Workspace per-org dirs | `agent/workspace.py::_workspace_path` |
| Queue per-org cap | `orchestrator/queue.py::_org_at_concurrency_cap` |
| Rate-limit task create | `orchestrator/router.py::create_task` (search `enforce_task_create_limit`) |
| BLOCKED_ON_QUOTA in lifecycle | `agent/lifecycle/coding.py`, `planning.py`, `review.py`, `conversation.py` (search `QuotaExceeded`) |
| Auto-unblock sweep | `orchestrator/unblock.py` |
| Run-loop wiring | `run.py` (search `unblock_quota_paused`) |
| Usage summary endpoint | `orchestrator/router.py` (search `/usage/summary`) |
| Usage UI page | `web-next/app/(app)/settings/usage/page.tsx` |
| Phase 3 handover | `docs/superpowers/plans/2026-05-11-handover-after-phase-3.md` |
| Settings sidebar | `web-next/app/(app)/settings/layout.tsx` |

---

## Out of scope (intentionally deferred)

- **Billing / Stripe / paywall** — Phase 5.
- **Usage notifications at 80% / 100% of cap** — UX nicety; not gating.
- **Weighted round-robin queue dispatch** (Pro orgs prioritized) — premature optimization; revisit on signal.
- **Monthly spend caps** — daily is the v1 unit.
- **Per-user (not per-org) caps** — orgs are the billing boundary.
- **Daily-rolled-up `usage_summaries` table** — for v1 we read raw `usage_events`. Add when per-org summary queries get slow.
- **Per-org Redis counters for rate limits** — Postgres aggregates fast enough today.
- **Disk-quota enforcement on workspace directories** — per-org `du -sh` is queryable but not capped.
- **`agent.slack_assistant.converse` per-org system token threading** — carried forward from Phase 3 out-of-scope list.
- **testcontainers real-DB suite** — still recommended; the per-test fixture in conftest is a reasonable middle ground.
- **`taskstatus` enum casing cleanup migration** — see "Critical things to know"; future task to normalize.

---

## Conversation summary

**Session arc (Phase 4):**

Phase 4 began by reading the Phase 3 handover and the multi-tenant implementation spec's §Phase 4. The user accepted the inherited risk list (testcontainers deferred, Phase 2/3 deploys still pending) and asked for execution. A 22-task implementation plan was written across 9 tracks (A–I), then executed via subagent-driven development with spec-compliance and code-quality review after each major task.

Four notable course-corrections during execution:

1. **`tests/helpers.py` import deferral** (after A0): the original spec had top-level `from shared.models import Plan, ...` in helpers.py, which fails until A2 lands. Fix: moved model imports inside the function bodies so module load is decoupled from when `Plan` actually exists. Caught by the code reviewer pre-A2.

2. **Migration 029's `taskstatus` enum case** (after A2): the original migration added `'blocked_on_quota'` lowercase, mirroring migrations 003/022. The code reviewer caught that the ORM serializes enum members by NAME (uppercase), so the lowercase value would be dead. Fix: added `'BLOCKED_ON_QUOTA'` uppercase (the variant the ORM queries) alongside the lowercase one. Also exposed a long-standing inconsistency in the codebase enum (see "Critical things to know").

3. **C1's session fixture event-loop issue**: `tests/conftest.py::session` originally used the global `shared.database.async_session` factory, whose AsyncEngine was bound to the first event loop pytest-asyncio created. Subsequent tests saw `InterfaceError: cannot perform operation: another operation is in progress`. Fix: rewrote the fixture to create a fresh `AsyncEngine` per test bound to the test's event loop.

4. **A4 downgrade test was non-hermetic**: the round-trip test ran `upgrade 029 → downgrade 028`, leaving the DB at 028 mid-suite. All subsequent DB-touching Phase 4 tests then failed with `relation "plans" does not exist`. Fix: wrap the downgrade body in `try/finally` and re-upgrade to 029 on exit.

The other major surprise was the `agent/loop.py → orchestrator/usage.py` module-boundary violation found by C3's code reviewer. Fix was straightforward: move `emit_usage_event` to `shared/usage.py` (it only depended on `shared/`).

**Final verification:** 845/845 tests passing (up from 808 at Phase 3 merge target, +37 new). Ruff at parity with Phase 3 baseline (207 errors, all pre-existing). `tsc --noEmit` clean for `web-next/`.

If anything in this document contradicts what you see in the code, **trust the code** and update this doc.
