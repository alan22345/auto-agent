# [ADR-019] Per-repo project secrets vault — architect-declared, user-managed, build-gating

## Status

Proposed

Extends ADR-018 §6 (per-domain ADR approval) and ADR-015 §11 (verify primitives). Does not supersede any existing ADR.

## Context

The scaffold flow described in ADR-018 produces real codebases. Real codebases need real third-party credentials at runtime: `STRIPE_API_KEY`, `POSTGRES_URL`, `OPENAI_API_KEY`, OAuth client secrets, SMTP creds, S3 access keys.

Today auto-agent has no concept of project secrets. The audit:

- `shared/secrets.py` exposes a closed allowlist `SECRET_KEYS = frozenset({"github_pat", "anthropic_api_key"})`. These are auto-agent's own credentials for talking to GitHub and the Anthropic API. The schema is `(user_id, organization_id, key)` — per-user, not per-repo.
- `agent/lifecycle/verify_primitives.py::boot_dev_server` (`l. 276`) does `env = os.environ.copy(); env["PORT"] = ...` and shells out. There is no per-project env layer.
- `auto-agent.smoke.yml` has `boot_command` and `expected_shape` but no `required_env` declaration.
- ADR-018 doesn't mention secrets at all.

The observable consequences:

1. **The domain architect writes "consumes `STRIPE_API_KEY`" into the ADR prose, but nothing surfaces this to the user as a setup step.** The user discovers the requirement when a Phase E final-verification round fails.
2. **The child trio writes code that reads `os.environ["STRIPE_API_KEY"]`.** At boot time, the value isn't there. The dev server crashes; the reviewer reports `gaps_found`; a gap-fix child trio is spawned to "handle missing env" — which is solving the wrong problem (the right answer is to set the env var, not to defensively code around its absence).
3. **Accidental cross-contamination.** Today, because `boot_dev_server` inherits the orchestrator's `os.environ`, a scaffolded project that happens to reference `ANTHROPIC_API_KEY` will "work" using auto-agent's own key. That is wrong on both axes: it hides the missing-credential problem from the user *and* leaks auto-agent's operational credentials into projects we're building.
4. **No way to set a key without going through an LLM.** The user explicitly does not want to type `"set STRIPE_API_KEY=sk_..."` into a chat that's recorded, summarised, and possibly memorised. They want a Vault-style UI.

Two related needs from the user:

- A first-class place to set/read/clear project credentials per repo, without sending the value through the agent chat surface.
- Architects declaring "this project needs these keys" as structured data — so the UI can nag the user, the build can gate on it, and the architect can know which keys are already set before re-asking.

## Decision

**Add a per-repo encrypted secret store, a structured architect-emitted required-secrets manifest, a hard gate between scaffold Phase C and Phase D until required secrets are populated, an agent-side read API, and boot-time injection into the project's runtime environment.**

### 1. Per-repo secret store

New ORM model `RepoSecret` in `shared/models.py`:

```python
class RepoSecret(Base):
    __tablename__ = "repo_secrets"
    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id"), index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    key: Mapped[str]                   # uppercase snake_case, e.g. STRIPE_API_KEY
    value_enc: Mapped[bytes]           # pgp_sym_encrypt(value, settings.secrets_passphrase)
    source: Mapped[str]                # 'user' | 'architect_required'
    purpose: Mapped[str | None]        # human-readable explanation, populated when source=architect_required
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    __table_args__ = (UniqueConstraint("repo_id", "key"),)
```

Free-form `key`, **no closed allowlist** — unlike `shared/secrets.py`. The whole point is to let a domain architect declare any credential the project happens to need.

Encryption reuses the existing `pgcrypto` pattern: plaintext is bound as a query parameter, `pgp_sym_encrypt` runs inside SQL with `settings.secrets_passphrase`, the plaintext never lives in a long-running Python variable. The Python layer is dumb shuffling — Postgres owns the crypto. This matches `shared/secrets.py` and means we depend on the same `SECRETS_PASSPHRASE` env var (already in production).

New service module `shared/repo_secrets.py` exposes `set`, `get`, `delete`, `list_keys` (returns names + `set` flag + source, **never values**), and `get_all_for_boot` (returns the full `{key: value}` dict — used only by `boot_dev_server` and the workspace `.env` writer, never by HTTP).

### 2. HTTP API

New endpoints in `orchestrator/router.py`, mirroring the `/me/secrets` pattern:

| Verb | Path | Returns |
|---|---|---|
| `GET` | `/repos/{repo_id}/secrets` | `[{key, set, source, purpose, updated_at}]` — no values |
| `PUT` | `/repos/{repo_id}/secrets/{key}` | `{ok, cleared}` — `source` becomes `'user'` if the row didn't exist, otherwise unchanged |
| `DELETE` | `/repos/{repo_id}/secrets/{key}` | 204 |
| `POST` | `/repos/{repo_id}/secrets/{key}/reveal` | `{value}` — requires explicit confirmation in the UI, audit-logged via structlog with `event=secret_reveal, key, repo_id, user_id` |
| `POST` | `/repos/{repo_id}/secrets/{key}/test` | optional connectivity probe; only implemented for keys whose `purpose.test_kind` matches a known prober (initial set: `postgres_url`, `stripe`) |

Authorization: caller's `current_org_id` must equal `repo.organization_id`. Values are never returned by the list endpoint and never logged in structured form (`shared/logging.py` adds `key` and `value_enc` to its redact list).

### 3. Architect-emitted required-secrets manifest

New skill `submit-required-secrets` writes one file per domain to the scaffold workspace:

```
.auto-agent/required_secrets/<domain-slug>.json
{
  "domain": "billing",
  "secrets": [
    {"key": "STRIPE_API_KEY", "purpose": "Charge cards via Stripe", "test_kind": "stripe"},
    {"key": "STRIPE_WEBHOOK_SECRET", "purpose": "Verify Stripe webhook signatures"}
  ]
}
```

Validators (new `agent/lifecycle/scaffold/required_secrets.py`):

- `key` must match `^[A-Z][A-Z0-9_]*$` (env-var convention, prevents collisions with shell-special names).
- `purpose` non-empty, ≤120 chars.
- `test_kind` optional, must be in the known prober set or `null`.
- No duplicate keys within a single domain manifest. (Cross-domain duplicates are allowed — multiple domains can need the same `POSTGRES_URL`. The dispatch-time union dedupes.)

Domain architects emit this **alongside** their domain ADR. The skill is invoked from the same Claude Code session as `submit-domain-adr`. If a domain needs no secrets, the architect skips the skill (the file's absence is interpreted as "no requirements"). The validator does not require every domain to declare; it only validates declarations that exist.

Each `submit-required-secrets` call **overwrites** the prior manifest for that domain. There is no merge with previous versions — a revise round whose new ADR no longer needs Stripe simply emits a new manifest without `STRIPE_API_KEY`. Manifests are never deleted by the architect; a manifest file persists until a new one replaces it, or until the domain is dropped from the build set (Phase C-gate `rejected`), in which case the parent driver deletes the file.

**Reconcile step on every manifest write.** The DB rows in `repo_secrets` track the **current** state of architectural demand, not a snapshot from first dispatch. When a domain manifest is (re)written, the parent driver runs a reconcile pass:

- Compute the *current union* of declared keys: `declared = union(keys across every domain's current manifest on disk)`.
- For every `RepoSecret` row whose `key` is in `declared`: ensure `source = 'architect_required'`. If a row had `source = 'user'` and its key newly appears in a manifest, promote it and populate `purpose` from the manifest entry.
- For every `RepoSecret` row whose `source = 'architect_required'` but whose `key` is **not** in `declared`: demote to `source = 'user'`, clear `purpose`. The value is preserved — the user still has whatever they typed; it's just no longer architect-required and no longer gates the build.

Concretely: round 1 declares `STRIPE_API_KEY`, user sets it, build dispatches. Round 2 (revise) pivots to Paddle: the new manifest contains `PADDLE_API_KEY` and not `STRIPE_API_KEY`. After reconcile, the `STRIPE_API_KEY` row demotes to `source='user'` (you keep the value, the UI moves it to "Other secrets"), and `PADDLE_API_KEY` becomes the new requirement. If you hadn't set Paddle yet, the build re-blocks; if you had, it just continues. No manual cleanup.

**The architect sees what's already set before writing.** The domain-architect prompt template (`agent/lifecycle/scaffold/prompts.py::DOMAIN_ARCHITECT_SYSTEM`) is extended to include the current manifest of populated secret keys (names only, never values) for this repo, so the architect:

- Doesn't ask for keys the user has already supplied ad-hoc.
- Can build on user-supplied keys as fixed inputs (e.g. "user has set `POSTGRES_URL` — design around that connection string format").

### 4. Phase C-to-D hard gate

Today, ADR-018 §6 says the scaffold parent transitions `AWAITING_DOMAIN_ADR_APPROVAL → DISPATCHING_DOMAIN_BUILDS` once every domain ADR has a non-`revise` verdict. This decision adds an intermediate status.

New scaffold-parent status `AWAITING_REQUIRED_SECRETS` (added to `orchestrator/state_machine.py`):

- Transition `AWAITING_DOMAIN_ADR_APPROVAL → AWAITING_REQUIRED_SECRETS` is automatic once every approved domain has been processed.
- At that point, `dispatch_children.py` queries `RepoSecret` for the repo. The gate is satisfied iff **every** row with `source = 'architect_required'` has a non-null `value_enc`. (No JSON file re-union at gate-check time — the manifest-write reconcile step in §3 keeps the DB rows authoritative.) If any architect-required row is unpopulated, the parent stays at `AWAITING_REQUIRED_SECRETS`.
- The endpoint `PUT /repos/{repo_id}/secrets/{key}` re-evaluates the gate for any scaffold parent of that repo currently parked at `AWAITING_REQUIRED_SECRETS`. If every architect-required row is now populated, transition `→ DISPATCHING_DOMAIN_BUILDS` and re-invoke `run_scaffold_parent`.
- A revise round that drops a previously-declared key also re-evaluates the gate: the §3 reconcile step demotes the row to `source='user'`, which removes it from the gate's required set; if that was the only blocker, the parent unblocks immediately without the user touching anything.
- A new endpoint `POST /scaffold/{task_id}/recheck-secrets` exists as a manual re-poke, but the auto-triggers on `PUT` and on manifest-write cover the common paths.

There is **no override** from this gate. If the user wants to skip a key (e.g. "I'll do Stripe later, just dispatch the auth domain"), they revise the offending domain ADR so the architect drops the requirement. This is intentional: silent skips lead to dispatched code that crashes at boot, which is the failure mode we're explicitly trying to remove.

Phase C-gate approval UX (web-next) is updated per ADR-018 §6: each domain ADR's row now shows two ticks instead of one — `[✓] approved` and `[✓] secrets populated`. A domain can be approved while its secrets are still missing; both must be green before that domain's child trio dispatches in Phase D.

### 5. Agent tools

Two new tools in `agent/tools/secrets.py`, registered when `ToolContext.repo_id` is set (i.e., the agent is operating inside a repo workspace; scaffold-parent-level agents like the root architect and intent-grill agent get them too, since they're tied to the repo from creation).

**`list_repo_secrets`** — returns the equivalent of `GET /repos/{id}/secrets`. No values. Available to every agent that has `repo_id`. This is the canonical "what's already set?" tool the domain architect uses (per §3).

**`get_secret`** — returns the value for a single key. Available to every agent that has `repo_id`. The user explicitly accepted that values land in the model's context, prompt cache, and Anthropic/Bedrock request logs in exchange for letting the agent write config files that embed real credentials (e.g. seeding a `prisma/schema.prisma` `DATABASE_URL` line).

`shared/logging.py` is extended to redact any string in `RepoSecret.value_enc` plaintexts from structured log output. We can't redact what Anthropic and Bedrock log on their side — that's the trade-off baked into "names + values via tool".

The tools are not exposed via the FastAPI HTTP layer to the agent; they go through `ToolRegistry`. There is no path by which a value transits the WebSocket event stream.

### 6. Boot-time injection

`agent/lifecycle/verify_primitives.py::boot_dev_server` (`l. 276`) is updated:

```python
env = _filtered_host_env()                       # strips orchestrator-scope keys
project_secrets = {}
if repo_id := getattr(workspace, "repo_id", None):
    project_secrets = await repo_secrets.get_all_for_boot(repo_id)
env.update(project_secrets)                      # project secrets win on collision
env["PORT"] = str(env_port)                      # PORT is set last; non-negotiable
```

Two rules, both load-bearing:

1. **Strip orchestrator credentials from the inherited host env before merging.** The orchestrator's `os.environ` contains keys that exist for auto-agent's own operation: Bedrock auth, Anthropic API key, the secrets passphrase, the orchestrator's own Postgres/Redis URLs, GitHub PAT, integration tokens. If we let those leak into a scaffolded project's runtime, a project that happens to read `ANTHROPIC_API_KEY` ends up using auto-agent's operational key. That is wrong on every axis: the user can't audit it, can't rotate it, and a scaffolded SaaS could quietly bill against the auto-agent owner's Anthropic spend.

   The deny-list is **derived from the field names of `shared.config.Settings`**. Any key that `Settings` reads from env is, by definition, an orchestrator-scope key. A new module function `shared/config.py::reserved_env_keys()` returns the upper-cased field name set; `_filtered_host_env()` does `{k: v for k, v in os.environ.items() if k.upper() not in reserved_env_keys()}`. Deriving from `Settings` rather than hand-maintaining a list means every new setting field is automatically protected — the deny-list cannot drift behind the codebase.

2. **Project secrets win on collision** (`env.update(project_secrets)`, not `setdefault`). After step 1 the only collisions would be benign host shell variables (`HOME`, `PATH`, `LANG`, `NODE_VERSION` from nvm, `PYENV_ROOT`, etc.); we want a project that declares e.g. `DATABASE_URL` to overwrite anything carried in from the orchestrator's shell. `PORT` is set last so it always wins, preventing a project from declaring `PORT` and breaking the verify_primitives health probe.

The same `_filtered_host_env()` helper is used by `agent/tools/bash.py` when it shells out from within a scaffolded workspace — same reasoning: nothing the orchestrator owns should be visible to scaffolded code.

### 7. Workspace `.env` write

`agent/workspace.py` (the clone/checkout helper) is extended: after `git clone` finishes, if the workspace has a `repo_id` and the repo has any `RepoSecret` rows, write `.env` at workspace root with `KEY=value` lines, one per row. Also ensure `.env` appears in `.gitignore` (append if absent, idempotent).

The write happens on every session start, not just first clone — the user may have added secrets between sessions, and the `.env` must reflect current state. The file is overwritten wholesale (not merged with existing content) to prevent stale rows from lingering after a user clears a secret.

This satisfies the "write to .env so frameworks like Next.js / FastAPI pick it up via dotenv" requirement without requiring the agent to call `get_secret` for each key.

### 8. PO standin behaviour in freeform mode

`agent/po_agent.py` gains no new responsibilities for secrets — the PO does **not** answer for the user on credential population. In freeform mode, if a scaffold parent reaches `AWAITING_REQUIRED_SECRETS` and the user has not populated keys, the parent simply blocks indefinitely (with a UI banner and an event). This is the one place where freeform mode is **not** fully autonomous: real secrets cannot be invented by an LLM standin without compromising both correctness and security. The PO standin (`po_approve_domain_adr`) still rubber-stamps the architectural approval; the secrets sub-gate is human-only.

### 9. Web-next UI

New page `web-next/app/(app)/repos/[repo]/secrets/page.tsx`:

- Two sections, both lists of `{key, set, source, purpose, updated_at}`:
  - **"Required by architects"** — entries with `source = 'architect_required'`. Missing values shown red with a [Set] button; populated ones shown green with [Edit] / [Clear] / [Test] buttons. `purpose` rendered as helper text.
  - **"Other secrets"** — entries with `source = 'user'`. Same actions plus an editable `key` field.
- "+ Add secret" CTA at the top opens a modal: free-form `key` (validated client-side against `^[A-Z][A-Z0-9_]*$`) + `value` (write-only). Submitting creates a `source = 'user'` row.
- Values are always masked. A "👁 reveal" button on each row triggers `POST /repos/{id}/secrets/{key}/reveal` with a 2nd-click confirmation. Reveals are audit-logged via structlog only (`event=secret_reveal, user_id, repo_id, key, ts`); no event is published on the WebSocket bus. Rationale: this is currently a single-user deployment posture; real-time admin visibility of reveal events has no audience. If we ever onboard teammates, adding a `publish()` call alongside the structlog line is a one-line change.
- During a scaffold parent's `AWAITING_REQUIRED_SECRETS`, the scaffold dashboard page (`app/(app)/tasks/[id]/page.tsx`) surfaces a banner:
  > "This build is waiting on **2 required secrets**. Set them to continue."
  > [Set required secrets →] (deep-links to the repo's secrets page filtered to `source=architect_required`).

A user adding an ad-hoc secret that happens to match the key an architect is about to declare causes no conflict — when the architect's manifest writes for that key, the existing row's `source` flips from `'user'` to `'architect_required'` and `purpose` is populated (only if previously null). The value is preserved.

### 10. Migration

Single Alembic revision `migrations/versions/<rev>_repo_secrets.py`:

- Creates `repo_secrets` table with the columns in §1.
- Unique constraint `(repo_id, key)`.
- Index on `repo_id`.
- No data migration needed — this is a brand-new model.

### 11. Tests

- `tests/test_repo_secrets.py` — CRUD against the encrypted store, round-trip plaintext via `pgp_sym_encrypt`/`pgp_sym_decrypt`, allowlist independence (free-form keys work).
- `tests/test_scaffold_required_secrets_validator.py` — manifest schema validation (key regex, purpose required, duplicate detection within a domain, cross-domain duplicates OK).
- `tests/test_scaffold_secrets_gate.py` — fan-in path: all domain ADRs approved + no required secrets ⇒ Phase D dispatches immediately; ADRs approved + 2 missing secrets ⇒ `AWAITING_REQUIRED_SECRETS`; `PUT /repos/{id}/secrets/{k}` re-evaluates and unblocks.
- `tests/test_verify_primitives_env.py` — `boot_dev_server` merges `RepoSecret` rows into the subprocess env; host `os.environ` keys returned by `reserved_env_keys()` do **not** leak through `_filtered_host_env()`; project secrets win on collision with benign host shell vars; `PORT` is preserved.
- `tests/test_workspace_dotenv.py` — `.env` is (re)written on every session start, `.gitignore` is appended idempotently, removed secrets disappear from `.env`.
- `tests/test_agent_secret_tools.py` — `list_repo_secrets` returns no values; `get_secret` returns value when set; both refuse without `repo_id` in `ToolContext`.

## Consequences

**Easier:**

- Scaffolded projects can run end-to-end against real third-party services without the user editing files on the orchestrator host.
- Architects can declare project dependencies as data, not prose — the UI can nag, the gate can block, the architect can see what's already set.
- The user can rotate a credential without re-running the build or talking to an LLM.
- Auto-agent's operational credentials (everything `shared.config.Settings` reads from env) are no longer accidentally inherited by scaffolded projects.
- The "PO standin can answer everything in freeform mode" model gets one explicit, defensible exception (§8) instead of pretending the PO can synthesise real API keys.

**Harder:**

- One more scaffold-parent status (`AWAITING_REQUIRED_SECRETS`) and another resume trigger (`PUT /repos/{id}/secrets/{k}` re-evaluates the gate). More moving parts in the lifecycle.
- Free-form key allowlist means the test/probe endpoints (§2) have a narrow set of known `test_kind`s. Anything else gets no connectivity test. Acceptable starting posture; expand the prober set as patterns emerge.
- Values land in agent context (per the user's choice in §5). Anthropic/Bedrock will log them server-side. structlog redaction protects our own logs but not external providers'. This is a deliberate trade-off, documented here so future-us doesn't re-litigate it without reading this paragraph.
- `.env` on disk is readable by any agent that can `cat` or `Read` it. The agent runtime has the `Read` tool and the `bash` tool, both of which can dump the file's contents into the model context. We accept this: it's the same trust boundary as `get_secret`, and refusing to write `.env` would break dotenv-based dev servers.

