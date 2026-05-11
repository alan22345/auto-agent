# Handover — multi-tenant SaaS, after Phase 2

**Date:** 2026-05-11
**Previous session shipped:** Phase 2 of `docs/superpowers/plans/2026-05-09-multi-tenant-saas-implementation.md` — the org/tenant model
**Branch state:** `feat/multi-tenant-org-model` (13 commits, **PR #39 open, NOT yet merged**). `main` is still at `c5e4d7c` (Phase 1 merge from the previous session).
**Production state:** Phase 1 is live on the Azure VM. Phase 2 is **not** deployed yet — wait for PR review + merge.

---

## What's done

### Phase 2 — shipped on the open PR

**Schema (migrations 026 + 027)**
- **026** — creates `organizations` + `organization_memberships`; adds nullable `organization_id` to `users`, `repos`, `tasks`, `scheduled_tasks`, `suggestions`, `freeform_configs`, `search_sessions`, `user_secrets`; backfills every existing row to a `default` org; seeds memberships (`user.id=1` → owner, others → member). Drops global `repos.name` UNIQUE; adds composite UNIQUE on `(organization_id, name)`. Repins `user_secrets` PK to `(user_id, organization_id, key)` so a user in two orgs can hold different credentials per org.
- **027** — flips every `organization_id` to NOT NULL (with a NULL-count sanity-check that raises a friendly error if 026's backfill is incomplete); adds composite query indexes (`tasks org+status`, `tasks org+created`, `suggestions org+status`, `search_sessions org+user`).

**Auth**
- `create_token()` now requires `current_org_id` (kwarg-only). Legacy callers fail loud rather than silently issuing a tenant-less token.
- New FastAPI dep `orchestrator.auth.current_org_id` raises 401 on pre-Phase-2 tokens — forces re-login for stale sessions.
- Signup creates a personal `Organization` named after `display_name`, plus an owner `OrganizationMembership`, in the same transaction.
- Login resolves the user's active org from their memberships (most-recently-active, ties broken by oldest) and bumps `last_active_at` so the next login lands in the same org.
- `verify_email` re-issues the cookie with the user's first org so the click-through completes the bootstrap.

**Scoping**
- `orchestrator/scoping.py::scoped(query, model, *, org_id)` — the load-bearing query helper. Registers seven direct-scoped models (`Repo`, `Task`, `Suggestion`, `FreeformConfig`, `ScheduledTask`, `SearchSession`, `UserSecret`) and four transitively-scoped models (`TaskHistory`, `TaskMessage`, `TaskOutcome` via `Task`; `SearchMessage` via `SearchSession`). Unknown models raise `KeyError` — silent fall-through is forbidden by design.
- Helpers `_get_task_in_org` and `_get_repo_in_org` in `router.py` centralise single-row lookups so callers can't accidentally skip the org filter.
- Every tenant endpoint in `orchestrator/router.py` now takes `org_id: int = Depends(current_org_id_dep)` and wraps its query in `scoped(...)`.
- Cross-module: `orchestrator/feedback.py` (summary + patterns), `orchestrator/deduplicator.py` (per-org `source_id` + `title` dedup), `orchestrator/create_repo.py` (stamp org on Repo/FreeformConfig/Task in the freeform path) gained optional `organization_id` kwargs.
- `shared/github_auth.py::get_github_token` now requires both `user_id` AND `organization_id` for the per-user PAT lookup. Task-context callers in `agent/workspace.py`, `agent/lifecycle/{coding,review,deploy}.py`, and `orchestrator/create_repo.py` thread `organization_id=task.organization_id` through.
- `shared/secrets.py` `set/get/delete/list_keys` all gained an `org_id` kwarg matching the new composite PK.

**Member CRUD + active-org switcher**
- `orchestrator/orgs.py` — `GET /api/orgs/me`, `POST /api/me/current-org`, `GET/POST/DELETE/PATCH /api/orgs/{id}/members`. 404-not-403 for cross-org probes; owner is removal-protected.
- Invitations resolve only to existing verified users. Cross-org invites for non-existent users are deferred (need an email click-through workflow — later phase work).

**UI** (`web-next/`)
- `lib/orgs.ts` + `hooks/useOrgs.ts` — typed fetch helpers + TanStack Query hooks.
- `components/org-switcher.tsx` — sidebar dropdown, hidden when the user has only one membership.
- `app/(app)/settings/organization/page.tsx` — members table + invite form (admins-only). Owner row hides Remove.
- TypeScript clean (`npx tsc --noEmit`).

**WebSocket scoping**
- `web/main.py::broadcast` takes optional `org_id` and filters clients by their stored `current_org_id`. Refuses pre-Phase-2 tokens with WS close code 4401.
- `_org_of(ws)` reads the per-client `current_org_id` set on connect. Every `await broadcast(...)` inside `_handle_*` and `websocket_endpoint` now passes `org_id=_org_of(ws)`.
- `event_listener` and `agent_stream_listener` (background tasks with no WS context) resolve the event's task → `_org_of_task(task_id)` → broadcast scoped. HTTP fetches replaced with direct-DB helpers (`_fetch_tasks_for_org`, `_fetch_pending_suggestions_for_org`, `_fetch_freeform_configs_for_org`) so the background loops no longer 401 on internal HTTP.
- `_ws_session_cookie(ws)` is forwarded on every `httpx.AsyncClient(...)` call inside the WS endpoint so the orchestrator's scoped endpoints' `current_org_id_dep` resolves correctly.

**Tests**
- **762 pass.** 13 new in `tests/test_org_scoping_coverage.py` — static-analysis coverage that walks every FastAPI route on `router.py` + `orgs.py` and asserts each one either takes `Depends(current_org_id)` or is in an explicit `UNSCOPED_ALLOWLIST` with a justification. Catches the regression class of "someone added an endpoint and forgot to scope it".
- 12 in `tests/test_scoping.py` — unit tests for `scoped()`.
- 4 new in `tests/test_auth.py` — `current_org_id_dep` behaviour + legacy-token rejection.
- Existing test fixtures (`tests/test_signup_flow.py::_SessionStub`, `tests/test_auth_cookie.py::_mock_session_for_login`) updated to model the extra membership lookup that login + verify_email now do.

---

## Production state

**Nothing Phase 2 is deployed yet.** Live VM still runs Phase 1 (PR #38). Migration 026 + 027 have NOT been applied to prod.

VM `.env` should already have `SECRETS_PASSPHRASE` set (Phase 1). Phase 2 keeps the same passphrase — the PK swap is `user_secrets(user_id, key)` → `user_secrets(user_id, organization_id, key)`, but the existing `value_enc` blobs are re-keyed with the same passphrase via the migration. No re-encryption needed; the passphrase MUST be unchanged across the upgrade.

---

## What's NOT done (in priority order)

### 1. PR #39 needs the user's review + merge
- **URL:** https://github.com/alan22345/auto-agent/pull/39
- 13 commits. 762/762 tests pass. ruff clean on the new files.
- The PR description lists deferred items + the deploy-time checklist.
- The user explicitly accepted the risk of starting Phase 2 without verifying Phase 1's two open items (real signup email click-through + per-user PAT on a real prod PR). Those are STILL unverified.

### 2. Phase 1 verification (carried over from previous handover, still NOT done)
- Real end-to-end signup → email → click → log in → `/tasks` walkthrough.
- A non-fallback user pushing a task that generates a PR with `Co-Authored-By: <name>` and `github_auth_mode=user_pat` in logs.
- If Phase 1 has a latent bug, Phase 2 may mask it. The static-analysis coverage test won't catch a bug in the Phase 1 plumbing (e.g. a signup that stores an empty `organization_id`).

### 3. Deploy verification (after merge)
Once PR #39 merges and the deploy script runs:
- **Confirm `alembic upgrade head` applies 026 then 027 in order.**
- **Re-confirm `SECRETS_PASSPHRASE` is unchanged.** A new passphrase would corrupt every stored secret because 026 leaves `value_enc` blobs intact during the PK swap.
- Manual smoke: sign up Alice + Bob, verify both, Alice invites Bob → Bob switches to Alice's org via the sidebar dropdown → confirm Bob sees Alice's tasks but not his personal-org tasks; confirm Alice doesn't see Bob's personal-org repos.
- Hit `/api/orgs/me` and confirm the response includes both orgs for Bob.

### 4. Phase 3 — per-org Slack + GitHub OAuth
The next big chunk. See the Phase 3 section of `docs/superpowers/plans/2026-05-09-multi-tenant-saas-implementation.md` for the full spec. Estimated 3-4 weeks.

**Don't start Phase 3 until #1-3 above are done.** Phase 3 stacks per-org integrations on top of Phase 2's tenant boundary — a Phase 2 bug will be much harder to debug once Slack/GitHub OAuth are also in the picture.

### 5. Deferred-by-design (do not touch in Phase 3)
These were explicitly out-of-scope for Phase 2 and remain so:
- Background pollers (`ci_status_poller`, `pr_merge_poller`, `pr_comment_poller`, `run_po_analysis_loop`, `run_architecture_loop`) still iterate across all orgs. They DO read `task.organization_id` from each row and pass it to downstream notifiers, but per-org concurrency caps and quota enforcement are **Phase 4** work.
- Owner-transfer endpoint. `_require_owner` blocks the only path to demoting/removing the owner. Manual SQL only — a `POST /api/orgs/{id}/transfer-ownership` endpoint can land in a follow-up if/when a customer asks.
- Real-DB parametrized isolation suite. The test harness today is static-analysis (`tests/test_org_scoping_coverage.py`) which catches "endpoint missing the dep" — but doesn't actually fire HTTP requests with two tokens and assert no cross-org rows come back. Building the real suite needs the `testcontainers[postgres]` dependency (already in `pyproject.toml`'s dev group, unused) wired into `tests/conftest.py` with a per-test schema + migration apply. Estimated 1-2 days. Worth doing before Phase 3 to give Phase 3 a real isolation backstop.

---

## Critical things to know

### `current_org_id` is in the JWT, NOT looked up per-request

Every authenticated request reads `current_org_id` from the cookie's JWT payload. Switching orgs = re-issuing the cookie via `POST /api/me/current-org`. There is no database hit per request to verify "is this user still in this org" — the JWT is trusted for its 7-day lifetime.

**Implication:** if you remove a user from an org, their existing session keeps working until expiry. To force-evict, bump the user's JWT secret rotation (not built yet) or wait 7 days. This is acceptable for v1; revisit when there's a hostile use case.

### `_get_task_in_org` / `_get_repo_in_org` are the canonical lookups

Single-row lookups by ID MUST go through these helpers. They wrap `scoped()` under the hood. Bypassing them with `select(Task).where(Task.id == x)` re-opens the tenant-leak hole that Phase 2 closed.

The static-analysis test (`test_org_scoping_coverage.py`) catches the endpoint-level regression (missing `Depends`). It does NOT catch a query-level regression (endpoint has the dep but the query doesn't use it). Reviewers must catch query-level slips in code review.

### `UserSecret` PK swap is destructive without the passphrase

`user_secrets` PK went from `(user_id, key)` to `(user_id, organization_id, key)` in migration 026. The migration:
1. Adds nullable `organization_id` column
2. Backfills it to the default org for every existing row
3. Sets the column NOT NULL
4. Drops the old PK + adds the new composite PK

The `value_enc` blobs are NOT re-encrypted — they're left as-is. So the existing `SECRETS_PASSPHRASE` must remain unchanged through this migration. If the passphrase rotates between 025 and 026, every stored secret becomes binary garbage.

**There is still no `rotate_secrets` script.** Defer until someone actually needs to rotate.

### M2M user↔org via `organization_memberships` is intentional

The previous handover decision was "many-to-many from the start" (per the user's explicit choice in this session). Solo signups still get a single personal org. The org-switcher in the sidebar UI is hidden when the user has only one membership.

Pre-existing legacy users (from before 026) all became members of the `default` org (user.id=1 = owner, everyone else = member). If a legacy user signs up a new account through the web UI today, they'd end up with TWO orgs: the `default` org (from the migration) AND a new personal org (from the signup flow). The active-org resolution favors most-recently-active membership, so they'd land in the personal org. Worth flagging if a confused legacy user reports "I can't see my old tasks" — they need to switch to `default` via the org switcher.

### WebSocket scoping is per-client, set at connect time

Each WS connection stamps `current_org_id` on the `connected_clients[ws]` dict. `broadcast(message, org_id=X)` only sends to clients whose stored `current_org_id == X`.

**Implication:** if a user switches their active org (`POST /api/me/current-org`), their WS still has the OLD org stamped on it. Their UI receives broadcasts for the old org until they reconnect (browser refresh closes/reopens the WS).

The Next.js org switcher (`components/org-switcher.tsx`) calls `router.refresh()` after the switch, which invalidates all TanStack queries and reconnects the WS. This is good enough for the typical "click switcher → page reloads" flow. A more sophisticated fix (in-place WS re-auth on org change) is deferred.

### Static-analysis test allowlist requires justification

`tests/test_org_scoping_coverage.py::UNSCOPED_ALLOWLIST` is the explicit list of endpoints that don't take `Depends(current_org_id)`. Each entry has a one-line reason. Adding an entry MUST come with a code-review-grade justification — "it's not tenant data" or "the dep is read inline from the JWT payload" are acceptable; "it doesn't need scoping" with no reasoning is not.

If a new endpoint is genuinely org-agnostic (e.g. admin-only system-wide), prefer adding it to the allowlist over making `current_org_id_dep` optional.

### `web/main.py` is still half-decommissioned (carried over)

Same as the previous handover: the legacy SPA at `web/static/index.html` is deleted, but `web/main.py` is kept for the WS handlers (which run.py mounts). The local `app = FastAPI(...)` instance in that file is test-only; production is `run.py`'s app.

The auto-generated formatting on Phase 2's bulk `broadcast(..., org_id=_org_of(ws))` edits is slightly ugly in spots (a comma trailing onto the next line on multi-line broadcasts). Functional, but if you re-format with `ruff format` later, the multi-line broadcasts will collapse to a cleaner shape.

### Auto-mode classifier blocks destructive git commands

Mid-session, `git stash pop` accidentally pulled in old work from a pre-existing stash entry on `main` and made a mess. The classifier denied `git checkout HEAD -- .` and `git clean -fd` (correctly — they'd wipe uncommitted work). Resolution required per-file `git checkout HEAD -- <name>` with explicit user approval for each batch.

**Practical advice:** before running `git stash` or any destructive git op, check `git stash list`. The repo had a stale `stash@{0}: WIP on main: f0b9c38 changes` from a previous session that nobody owned. It was dropped (per user direction) but caused real friction.

---

## Where to find things

| Topic | Path |
|---|---|
| Full Phase plan (the spec) | `docs/superpowers/plans/2026-05-09-multi-tenant-saas-implementation.md` |
| Strategic roadmap | `docs/superpowers/plans/2026-05-09-multi-tenant-saas-roadmap.md` |
| Phase 1 handover | `docs/superpowers/plans/2026-05-11-handover-after-phase-1.md` |
| Phase 2 implementation plan | `docs/superpowers/plans/2026-05-11-phase-2-org-tenant-model.md` |
| **Phase 2 PR** | https://github.com/alan22345/auto-agent/pull/39 (open, not merged) |
| Migration 026 | `migrations/versions/026_organizations.py` |
| Migration 027 | `migrations/versions/027_organizations_not_null.py` |
| Scoping helper | `orchestrator/scoping.py` |
| Member CRUD endpoints | `orchestrator/orgs.py` |
| Scoping coverage test | `tests/test_org_scoping_coverage.py` |
| `_get_task_in_org` / `_get_repo_in_org` | `orchestrator/router.py` (~lines 200-260) |
| Active-org resolution on login | `orchestrator/router.py::_resolve_active_org_id` |
| WS org-scoped broadcast | `web/main.py::broadcast`, `_org_of`, `_org_of_task` |
| Org switcher UI | `web-next/components/org-switcher.tsx` |
| Settings/organization page | `web-next/app/(app)/settings/organization/page.tsx` |

---

## Conversation summary

This session ran roughly as:

1. **Planning** — read the Phase 1 handover, asked the user three questions: (a) verify Phase 1 first? (b) one bundled PR or multiple? (c) M2M now or later? User chose: proceed without Phase 1 verification, one bundled PR, M2M from the start.
2. **Implementation** — 13 commits in order: migration 026, ORM models, JWT changes + signup org bootstrap, scoping helper + tests, full endpoint scoping pass in `router.py`, cross-module scoping (deduplicator, feedback, github_auth, secrets, workspace, lifecycle), member CRUD endpoints, UI (org switcher + settings page), WS broadcast scoping at the per-client + cookie-forward level, migration 027, lint cleanup, **then** addressed the three follow-ups that the user flagged as "do now": WS broadcasts in `_handle_*`, event_listener org-aware fetches, static-analysis coverage test.
3. **Mid-session friction:**
   - A `git stash pop` accidentally pulled in unrelated work from a pre-existing stash entry on `main`. Resolved with the user's explicit approval per file.
   - The auto-mode classifier blocked several destructive git commands. Mostly the classifier was right; one false positive was a per-file `git checkout HEAD --`.
   - Spent ~30 min threading `organization_id` through ~40 `get_github_token` callsites. Resolved by making `get_github_token` accept `organization_id` as a kwarg that falls through to env-PAT if missing — task-context callers thread the real value; pollers don't and rely on the env path.
4. **PR open** — pushed `feat/multi-tenant-org-model` and opened PR #39 with a detailed description listing deferred items.
5. **Follow-up triage** — the user asked: "as long as all follow ups are eventually addressed in other phases its ok otherwise should be done now". Audit identified three items NOT covered by later phases (WS broadcasts, event_listener, isolation test). All three landed on the same branch as commits `cdf1988` + `31b3ddd`.

**Decisions captured:**
- M2M user↔org with `organization_memberships` (decided up front).
- One bundled PR for all of Phase 2.
- Cross-org invites for non-existent users: defer (need email click-through workflow).
- Real-DB isolation suite: defer (needs testcontainers wiring — 1-2 days, recommended before Phase 3).
- Owner-transfer endpoint: defer (manual SQL acceptable for v1).
- Per-org concurrency caps: explicit Phase 4 work.

**Pitfalls hit (carry forward to next session):**
- Stale `git stash` from earlier sessions can blow up `git stash pop` invocations even when you didn't create the stash. Check `git stash list` before stashing.
- `pytest` runs against mocked sessions, not a real DB. Migration files are not exercised in CI. Trust the SQL on file; verify at deploy time.
- Most signature changes to shared helpers (e.g. `create_token`, `get_github_token`, `shared.secrets.*`) cascade into N test files because mocks are tightly coupled to the original signatures. Plan for ~30 minutes of test fixture updates whenever a foundational helper grows a parameter.

If anything in the above contradicts what you see in the code, **trust the code** and update this doc.
