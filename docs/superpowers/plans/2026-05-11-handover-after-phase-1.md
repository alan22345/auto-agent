# Handover — multi-tenant SaaS, after Phase 1

**Date:** 2026-05-11
**Previous session shipped:** Phase 0 + Phase 1 of `docs/superpowers/plans/2026-05-09-multi-tenant-saas-implementation.md`
**Branch state:** `main` is at `c5e4d7c` (merge of PR #38). `feat/multi-tenant-claude-credentials` deleted.
**Production state:** Live on Azure VM (`azureuser@172.190.26.82`), Next.js on `:3000`, FastAPI on `:2020`.

---

## What's done

### Phase 0 — closed out
- Per-user Claude credential vaults at `/data/users/{id}/.claude/`
- In-UI OAuth pairing flow at `/settings/claude`
- Dispatch-time auth probe → `BLOCKED_ON_AUTH` task state
- 5-slot global FIFO worker pool, per-repo cap of 1
- Fallback-user mode (`settings.fallback_claude_user_id`) — unpaired users transparently share an admin's vault
- **Co-Authored-By trailers** on agent commits (this session) — `commit-msg` hook installed in cloned workspaces at clone time
- **Auth-mode logging** (this session) — `github_auth_mode mode=user_pat|app|env_pat` emitted once per scope
- Stale PTY-era `tests/test_claude_pairing.py` deleted

### Phase 1 — shipped
- **Migration 025** — `user_secrets` table (pgcrypto-encrypted at rest), `users.email/email_verified_at/signup_token` columns, `pgcrypto` extension
- **`shared/secrets.py`** — `set/get/delete/list_keys` via `pgp_sym_encrypt/decrypt`. Closed allowlist (`github_pat`, `anthropic_api_key`). Refuses empty `SECRETS_PASSPHRASE`
- **`shared/email.py`** — Resend transactional email. Logs verify URL when `RESEND_API_KEY` is unset (dev fallback)
- **`shared/github_auth.py`** — `get_github_token(user_id=)` resolves per-user PAT → GitHub App → env PAT
- **`agent/llm/__init__.py`** — `api_key_override` on `get_provider` + `resolve_user_anthropic_key` helper for the AnthropicProvider path
- **Endpoints** — `POST /api/auth/signup`, `GET /api/auth/verify/{token}`, `PATCH /api/auth/me/email`, `GET/PUT/DELETE /api/me/secrets`, `POST /api/me/secrets/{key}/test`
- **Next.js pages** — `(public)/signup`, `(public)/verify/[token]`, `(app)/settings/github`, `(app)/settings/anthropic`, `(app)/settings/layout.tsx` (sidebar)
- **`user_id` threaded** through ~13 task-scoped call sites: `workspace.clone_repo`, `lifecycle/coding/review/deploy`, `conflict_resolver`, `freeform.promote/revert`, `create_repo`, `_fetch_pr_state`, `_auto_merge_pr`. Pollers stay org-level by design
- **Login** accepts email OR username; users with `email IS NOT NULL` must have `email_verified_at` set

### Decommissioned
- `web/static/index.html` — 3,438-line legacy SPA gone. Next.js on `:3000` is the only UI. FastAPI on `:2020` still serves `/api` and `/ws`; Next.js proxies via `next.config.js`
- Tests that grepped `index.html` (`test_chat_scroll_preservation`, `test_suggestion_expand`)

### Tests
- **732 pass.** 24 new: `test_secrets`, `test_signup_flow`, `test_github_auth_per_user`, `test_coauthor_hook`
- pgcrypto integration is exercised only via manual smoke. Unit tests mock the SQL layer

---

## Production state

**Deployed:** All of the above. Migration applied (`alembic current` = `025`).

**VM `.env` additions made:**
- `SECRETS_PASSPHRASE=<random hex, 32 bytes>` — generated on VM during this session. **The user does not have a copy.** If the VM loses this file, every stored `user_secrets.value_enc` becomes unrecoverable. Strongly recommend: SSH in, copy the value to a password manager, communicate to the user
- `APP_BASE_URL=http://172.190.26.82:3000`
- `RESEND_API_KEY=<the user's re_dCRGhjP4… key>`
- `RESEND_FROM="auto-agent <autoagent@ergodic.dev>"` — value is double-quoted because `bash source` (used by `scripts/deploy.sh`) interprets `<>` as redirects without quotes

**Test user pollution:** `users` table on prod has one row `deploy-smoke@example.com` (id=6, unverified) from the deploy smoke test. Safe to delete:
```sql
DELETE FROM users WHERE email='deploy-smoke@example.com';
```

---

## What's NOT done (in priority order)

### 1. Real end-to-end signup walkthrough by the user
The deploy smoke (`POST /api/auth/signup`) returned `verification_sent: true`, which only means Resend's HTTPS endpoint accepted the request. It does **not** prove:
- The Resend domain `ergodic.dev` is actually green-verified in their account (if not, the message bounces or sits in Resend's queue and never delivers)
- The verify link in the email is clickable and lands on the right page
- The whole flow (signup → email → click → `/tasks` logged in) works in a real inbox

**Ask the user to do this before declaring Phase 1 done.** If emails don't arrive, first check **Resend → Domains** for `ergodic.dev` status. The user *thought* the verified domain was `supyagent.com` earlier in the session, then asked for `bot@autoagent.com` (mid-confusion), then settled on `ergodic.dev`. Treat that ambiguity as unresolved.

### 2. Confirm per-user PAT actually works in production
Push a task as a non-fallback user and verify:
- `docker compose logs auto-agent | grep github_auth_mode` shows `mode=user_pat`
- The PR's commit messages contain `Co-Authored-By: <Display Name> <<username>@auto-agent.local>`
- The PR author on GitHub matches the user's stored PAT identity

### 3. Phase 2 — org/tenant model
The next big chunk. See `docs/superpowers/plans/2026-05-09-multi-tenant-saas-implementation.md` Phase 2 section for the full spec. Estimated 3-4 weeks.

**Don't start Phase 2 until step 1 + 2 above are confirmed working.** Phase 2 stacks org scoping on top of Phase 1's per-user scoping — if Phase 1 has a bug, it'll get masked by Phase 2's additional layer.

---

## Critical things to know

### `SECRETS_PASSPHRASE` is load-bearing
- Used as the symmetric key for `pgp_sym_encrypt` in `shared/secrets.py`
- Stored in VM `.env` only (gitignored, not committed)
- **There is no recovery path** if it's lost. Every encrypted `user_secrets.value_enc` becomes binary garbage
- Treat exactly like a DB encryption key: password manager, off-machine backup, document who has access
- Future work: add a `rotate_secrets` script for migrating to a new passphrase (decrypt with old, re-encrypt with new, in a transaction). Not built yet — defer until needed

### Pollers intentionally stay org-level
- `ci_status_poller`, `pr_merge_poller`, `pr_comment_poller` in `run.py` iterate over *all* active tasks
- They use `get_github_token()` with **no** `user_id` — falls through to GitHub App or env PAT
- If you remove `GITHUB_TOKEN` from `.env` on the VM, pollers will fail silently unless a GitHub App is configured
- Don't "fix" this by threading `user_id` through — the pollers don't have a single user context. Per-user polling is Phase 4 work

### Two Postgres on local Mac (will confuse you)
- The user's local Mac has both a Mac-native Postgres and a Docker Postgres
- Both bind `localhost:5432`; whichever starts first wins
- `.env`'s `DATABASE_URL` points at Mac-native (`alanyeginchibayev@localhost`)
- Docker Compose `environment:` overrides `.env` and points the container at the Docker service (`postgres:5432`)
- This means running `alembic` from the host venv vs inside the container hits different DBs
- The user does **not** test locally — they deploy to the VM. Don't waste time on local Docker. If you need to test schema changes, do it directly on the host venv against Mac-native Postgres

### macOS Claude auth doesn't bind-mount
- Claude CLI stores credentials in the macOS Keychain on Mac (NOT in `~/.claude/`)
- The docker-compose bind-mount of `~/.claude` and `~/.claude.json` carries the config but NOT the auth tokens
- So `claude auth status` inside the container will always fail on macOS dev
- This is why `shared/preflight.py` downgrades the Claude check to a WARNING (Phase 1 change)
- Production (Linux VM) is fine — there auth tokens do live on disk

### Bash `source .env` is fragile
- `scripts/deploy.sh` does `set -a && . .env && set +a` to load env vars
- Unquoted values with shell metacharacters (`<`, `>`, `;`, `&`, `|`, etc.) break the source
- This bit us during deploy with `RESEND_FROM=auto-agent <bot@autoagent.com>` — bash saw `<` as a redirect
- Fix: always quote values containing special chars in `.env`. Already done for `RESEND_FROM` on the VM
- Future cleanup: switch deploy script to a Python or `dotenv`-style loader that doesn't shell-interpret

### `web/main.py` is NOT fully decommissioned
- The legacy SPA at `web/static/index.html` is deleted
- But `web/main.py` is **kept** because it contains the WS handlers (`websocket_endpoint`, `event_listener`, memory upload, etc.) that the active Next.js UI relies on
- Don't be misled by the file name — half of it is current, half is historical
- The `app = FastAPI(...)` instance in `web/main.py` is only used by tests now; production is `run.py`'s app

---

## Where to find things

| Topic | Path |
|---|---|
| Full Phase plan (the spec) | `docs/superpowers/plans/2026-05-09-multi-tenant-saas-implementation.md` |
| Strategic roadmap | `docs/superpowers/plans/2026-05-09-multi-tenant-saas-roadmap.md` |
| **Phase 1 PR** | https://github.com/alan22345/auto-agent/pull/38 (merged) |
| Migration 025 | `migrations/versions/025_per_user_secrets.py` |
| Secrets store | `shared/secrets.py` |
| Resend wrapper | `shared/email.py` |
| GitHub auth resolution | `shared/github_auth.py` |
| Signup/secrets endpoints | `orchestrator/router.py` (search for `# --- Self-serve signup`) |
| Commit-msg hook | `agent/workspace.py::install_coauthor_hook` |
| Deploy script | `scripts/deploy.sh` |
| Project conventions | `CLAUDE.md` |

---

## Conversation summary

The previous session ran roughly as:
1. **Planning** — read the Phase 1 spec, clarified scope (full Phase 1 incl. email; open signup; one bundled PR; Resend not SES)
2. **Implementation** — 9 sub-tasks: migration, secrets module, email module, github_auth refactor, LLM api_key_override, endpoints, signup tests, UI pages, final verification
3. **Decommission of legacy `:2020` SPA** — added mid-session when the user noted it was deprecated
4. **Local testing snags** — wasted ~1 hour on local Docker DB state and macOS Claude auth before user clarified "I test on the VM, don't care about local"
5. **Deploy** — three rounds: blocked by missing `.env` vars, then by `RESEND_FROM` quoting, then succeeded
6. **Last verified** — `POST /api/auth/signup` returned 201 from prod, real email click-through NOT yet tested by the user

**Decisions captured along the way:**
- Email provider: Resend (the user has an existing account; one verified domain serves all sending)
- Signup model: open (anyone with an email)
- PR shape: one bundled PR
- Auto-app-base-URL detection: not feasible (background tasks have no request context); explicit env var required
- Resend `From:` configurable via env, not hardcoded — lets the user change without a deploy
- Soften `claude auth status` preflight to a warning (per-user pairing is the production path now)
- Pollers stay org-level (deferred to Phase 4)

**Pitfalls the previous session hit:**
- Two local Postgres instances on the same port → confused which DB alembic was migrating
- `alembic stamp head` against a half-migrated DB → made `Base.metadata.create_all` fail with "column does not exist"
- `bash source .env` choked on `<>` in `RESEND_FROM` → deploy script crashed mid-rebuild
- Sandbox blocked SSH-to-prod and merge-to-main in auto mode → had to wait for explicit user re-authorization

If anything in the above contradicts what you see in the code, **trust the code** and update this doc.
