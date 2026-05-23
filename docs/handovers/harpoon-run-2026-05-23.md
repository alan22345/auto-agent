# Handover: Harpoon Scaffold Run + "Build Something New" Bug Cascade

**Date:** 2026-05-22 → 2026-05-23 (~12 hours debugging across one session)
**Outcome:** Stopped mid-run. Designs landed (~400KB of high-quality engineering ADRs). Code never written. 13+ distinct bugs found in the SCAFFOLD path, most patched, deployed. One systemic issue (parallel coding across trio children) and one symptomatic issue (coders produce no diff) left open. Tasks 12–19 BLOCKED on the VM.

---

## 1. What the user wanted

The user invoked **"Build something new"** with a 29 KB GTM-outbound-automation product brief (project name "harpoon", under org `ergodic-ai`). The expectation:

1. The brief is the **full app design** — it lists every domain (mock vendors, schema, ICP translator, copywriter, validator, reply classifier, funnel, UI, webhooks), data model, locked architectural decisions, success criteria.
2. The system should **scaffold the entire app end-to-end** as 6–10 real feature domains with concrete code.
3. **Coding across children must be serial** — only one child trio writing code at any moment. Parallelism across children is "not correct functionality" (user's words).
4. Repository `ergodic-ai/harpoon` on GitHub. Description = product brief.

The user wants the brief preserved as the source of truth — see `docs/handovers/harpoon-brief.md` (local) and `repos.product_brief` for repo ids 237/238 on the VM (28.6 KB markdown brief stamped into both rows).

---

## 2. Where things stand on the VM right now

Status as of halt (Sat May 23 11:30 UTC):

| Task | Title | Status | Backlog | Notes |
|------|-------|--------|---------|-------|
| 12 | Scaffold: harpoon (SCAFFOLD parent) | BLOCKED | — | 7 domain ADRs written on disk |
| 13 | Domain build: Platform | BLOCKED | 11 items | Was BLOCKED before halt — coder no-diff failure |
| 14 | Domain build: Campaign Orchestrator | BLOCKED | 9 items | Halted while builder loop was firing |
| 15 | Domain build: Enrichment | BLOCKED | 8 items | Halted while builder loop was firing |
| 16 | Domain build: Drafting | BLOCKED | 10 items | Architect hung once (12hr ARG_MAX freeze); re-emitted backlog after restart |
| 17 | Domain build: Sequencing | BLOCKED | 9 items | Architect first emitted 3 meta-items only; re-triggered, produced 9 real slices |
| 18 | Domain build: Replies & Notifications | BLOCKED | 6 items | Halted while builder loop was firing |
| 19 | Domain build: Funnel SoR | BLOCKED | 11 items | Halted while builder loop was firing |

**Workspaces:** `/workspaces/1/task-{12..19}/.auto-agent/` contains 8 ADR docs (root + 7 domain) plus 7 grill summaries. Total ~400KB of engineering specs. Each workspace has its own git clone of `ergodic-ai/harpoon` on a per-task branch. Git stash entries exist from the workspace-reuse fixes.

**GitHub:** `ergodic-ai/harpoon` exists, has the auto-init `main` + per-domain branches from the architects' design commits. No feature code committed.

**Recovery infrastructure deployed (load-tested):** `scaffold_heartbeat_watchdog` + `resume_all_scaffold_parents` + `resume_all_trio_parents` (expanded). With BLOCKED status, none of these will re-pick the halted tasks.

---

## 3. The 13+ bugs found in this run, in order

### Pre-flight DB bugs (chronological)

1. **`taskcomplexity` enum case mismatch** — migration 046 added `'scaffold'` lowercase only; SQLAlchemy binds the Python enum name `SCAFFOLD` (uppercase). Result: `invalid input value for enum taskcomplexity: "SCAFFOLD"` on every Build-something-new POST. **FIX:** new migration `migrations/versions/054_normalize_046_051_enum_uppercase.py` adds uppercase variants for 046 + 051 values. Applied on VM. Convention violation by 046 / 051 — every future `ALTER TYPE … ADD VALUE` MUST add both `'name'` AND `'NAME'` (see `migrations/versions/052_normalize_enum_uppercase.py` for the canonical pattern).

2. **Orphan `repos` rows blocking retries** — `orchestrator/create_repo.py:264` triggers an autoflush during `session.execute(short_lookup_q)` that INSERTs `full_repo` *before* any conflict check. If a prior attempt persisted Repo rows (via partial commit, dependency-injected session commit, etc.) the next retry hits `UniqueConstraint('organization_id', 'name')`. **Not fixed systemically.** Recommended fix: pre-check `SELECT 1 FROM repos WHERE org_id=? AND name IN (full_name, short_name)` before adding anything to the session, and return early / reuse existing rows. We cleaned up orphans by hand 3+ times during the run.

3. **`TaskData.subtasks` Pydantic type too narrow** — declared `list[dict] | None`, but the SCAFFOLD parent stores progress counters as a dict (`{"scaffold": {"current_domain_idx": N}}`). Every `GET /api/tasks` 500'd whenever a scaffold task had populated subtasks → UI showed "No tasks yet". **FIX:** `shared/types.py:58` widened to `list[dict] | dict | None`. TS types regenerated. Tests still pass.

### Title / scope bugs

4. **Title extraction used raw first description line** — `create_repo.py:294` took `description.splitlines()[0]`. With a markdown brief starting `## 1. What this service is, in one paragraph`, the title became `Scaffold: ## 1. …` → the intent-grill agent inferred "section 1 of a series of scaffolds" → produced foundation-only intent → architect made 4 micro-domains (Project Identity, Stack ADR, Monorepo Skeleton, Repo Hygiene). **FIX:** new `_first_prose_line()` helper in `create_repo.py` skips markdown headers / numbered sections / blockquotes / fences. Caller-supplied `name` (from the UI form) wins as the title basis. 3 new tests in `tests/test_create_repo_name_generation.py`.

5. **`INTENT_GRILL_SYSTEM` prompt biased to minimum scope** — original prompt asked for "the smallest set that makes this thing recognisable as itself". Combined with a brief that lists `### Phase 1 / Phase 2 / …` as a "Suggested build order", the agent dutifully deferred everything outside Phase 1. **FIX:** `agent/lifecycle/scaffold/prompts.py` got a new "## Scope contract" section: "phases are HINTS about ORDER, not scope limits", "build the WHOLE app", "501 stubs and empty agent shells are NOT acceptable", "out-of-scope = only what the brief explicitly marks". 3 new regression tests in `tests/test_scaffold_intent_prompt.py` pin the contract. After this fix, intent.md correctly listed every feature as in-scope and out-of-scope only the brief's literal "not in v1" items.

### Freeform-mode wiring bugs

6. **Freeform PO standins not wired into scaffold parent driver.** `agent/lifecycle/scaffold/parent.py` had `request_po_verdict` / `request_po_verdicts` / `request_po_domain_answer` functions defined but **never called** — every `AWAITING_*` block just `return`ed. Freeform scaffolds stranded forever at the first gate (`AWAITING_ROOT_ADR_APPROVAL`). **FIX:** `_run_phase_with_retry`, `_notify_user`, `_handle_root_adr_gate_freeform`, `_handle_domain_adr_gate_freeform`, `_handle_domain_grill_gate_freeform` added to `parent.py`. Each `AWAITING_*` block now does `if freeform: standin + apply_verdict + continue; else: notify + return`. Pattern documented in `parent.py` line ~119.

7. **Freeform design-standin not invoked on first dispatch for trio children.** `_advance_through_design_gate` case D ran `architect.run_design`, transitioned to `AWAITING_DESIGN_APPROVAL`, then `return False`. The freeform standin (case B) only runs when the helper is *re-invoked* on a task already in that state. Nothing re-invoked the dispatcher for scaffold-children → every freeform child trio deadlocked at the design gate. **FIX:** `agent/lifecycle/trio/__init__.py:516`: after `architect.run_design`, reload task; if status is now `AWAITING_DESIGN_APPROVAL`, recurse `_advance_through_design_gate(refreshed)`. Recursion is bounded (case B either approves, rejects, or yields).

8. **PO standin returned silently when `parent.repo` was None.** `_try_freeform_design_standin` in `trio/__init__.py:552` short-circuited when the lazy ORM relationship wasn't loaded — which happens whenever the task is loaded outside an active session (e.g. by the recovery hook). **FIX:** fallback `SELECT * FROM repos WHERE id = parent.repo_id` if `parent.repo is None`. Logs `freeform_design_standin_no_repo` if even that fails.

9. **`Repo.mode` defaulted to `human_in_loop`.** `resolve_effective_mode(task, repo)` (called by `run_freeform_gate` in `agent/lifecycle/standin.py:638`) reads `Repo.mode` to decide whether to fire the standin. `create_repo.py` was creating `Repo(...)` without setting `mode`. The DB default is `human_in_loop`. Result: even with all the above fixes, every standin returned `False` from `resolve_effective_mode → human_in_loop`, and gates logged `Fallback: plan_approval:no_product_brief`. **FIX:** `create_repo.py` now stamps `mode="freeform"` on both `full_repo` and `short_repo`.

10. **`Repo.product_brief` not stamped on new repos.** The PO standins fell back to deterministic heuristics ("Default answer: approve") because there was no product brief to ground decisions in. Once the design gate verdicts are written, those fallback verdicts can't be re-rolled — the next attempt will inherit the lazy approval. **FIX:** `create_repo.py` now stamps `product_brief=description` on both Repo rows. Backfilled on VM via `UPDATE repos SET product_brief = (SELECT description FROM tasks WHERE id=12) WHERE id IN (237,238)`.

### Architect / agent runtime bugs

11. **`claude_cli` ARG_MAX overflow** — agent provider was `LLM_PROVIDER=claude_cli`. The prompt was passed as a CLI positional argv. Linux `ARG_MAX` (~128 KB) was exceeded once the architect's accumulated context (intent.md + root ADR + prior domain grills/ADRs + 112 KB sequencing grill) crossed the threshold. uvloop's `create_subprocess_exec` blocked indefinitely instead of raising → 12-hour silent freeze before any logs surfaced. **FIX:** `agent/llm/claude_cli.py:_invoke_cli_once` now pipes the prompt via stdin (`communicate(input=prompt.encode())`). No ARG_MAX ceiling. 2 regression tests in `tests/test_claude_cli_stdin.py` pin: prompt is NOT in argv, and IS sent via stdin.

12. **Architect produced meta-only backlog (sequencing).** task 17's architect emitted 3 items: "Extract backlog from design.md", "Commit architecture artifacts", "Enrich architect_log.md". Zero items built the SF client / push worker / webhooks / bulk-DNC. Root cause unclear — could be LLM noise on one run; could be a prompt issue surfacing for large grills (sequencing's grill was 83 KB). Same shape seen on task 16 (drafting) which hung mid-architect-run before emitting anything. After re-triggering, both produced real backlogs (9 and 10 items). **Not fixed systemically — re-trigger worked but the underlying instability is open.**

13. **Workspace prep not idempotent against re-runs.** `agent/workspace.py:create_branch` called `git checkout <branch>` which failed with "The following untracked working tree files would be overwritten by checkout: .gitignore" because the architect's prior run had left untracked files (`.auto-agent/`, `.gitignore`, `.venv/`, `__pycache__/`) and the destination branch had its own tracked `.gitignore`. **FIX:** (a) added idempotency check — if `current_branch == target`, return without doing a checkout; (b) defensive `git stash push --include-untracked` before any branch switch so the checkout never fails on overwrite. Stash is intentionally not popped — design.md is read by file path, not via git, so the next phase re-creates what it needs.

### Recovery / orchestrator gaps

14. **No trio recovery for `ARCHITECT_BACKLOG_EMIT` or `AWAITING_DESIGN_APPROVAL`.** Original `resume_all_trio_parents` only queried `TRIO_EXECUTING`. Children that landed at the design-approval gate or post-architect waiting-for-builder state could never recover from a container restart. **FIX:** `agent/lifecycle/trio/recovery.py` expanded to include `AWAITING_DESIGN_APPROVAL` (freeform only — the standin needs the dispatcher re-invoked) AND all `ARCHITECT_BACKLOG_EMIT` (the `has_backlog` short-circuit handles idempotency).

15. **No scaffold parent recovery at all.** SCAFFOLD parents stranded at `AWAITING_ROOT_ADR_APPROVAL` for 47 min before the user noticed. **FIX:** new `agent/lifecycle/scaffold/recovery.py` with `resume_all_scaffold_parents` (mirror of trio) + `scaffold_heartbeat_watchdog` (background task: every 5 min, scans for SCAFFOLD parents in active statuses with `updated_at` older than 90 min). Watchdog threshold raised from 30 → 90 min after a false-positive caused two concurrent drivers to race on transitions and produce `InvalidTransition: AWAITING_REQUIRED_SECRETS -> AWAITING_REQUIRED_SECRETS` errors. Recovery hook now bumps `updated_at` immediately on entry to close the race.

16. **`_handle_domain_adr_gate_freeform` was not idempotent across `apply_verdict` calls.** `request_po_verdicts` writes ALL 7 verdict files first; the FIRST subsequent `apply_verdict` call sees all-resolved and transitions to `AWAITING_REQUIRED_SECRETS`. Subsequent calls in my for-loop tried the same transition again and tripped `InvalidTransition`. **FIX:** between iterations, reload the task; if status has moved past `AWAITING_DOMAIN_ADR_APPROVAL`, break the loop.

### Open systemic issues at halt

17. **Parallel coding across child trios** — the trio dispatcher has no per-scaffold serializer. When recovery (or any event) fires multiple child trios simultaneously, each runs `dispatcher.dispatch_item` concurrently. The user explicitly stated this is incorrect: child trios under a scaffold parent must be serial. The monitor I built (`task bsx7p2afs`) filtered on `status IN ('TRIO_EXECUTING', 'CODING', …)` but the actual coder concurrency happens *inside* `dispatch_item` while the parent status is still `ARCHITECT_BACKLOG_EMIT` — so the monitor missed it. **Not fixed.** Recommended approaches: (a) add a `scaffold_serializer` lock keyed on `parent.parent_task_id` (the SCAFFOLD root) — only one of the children's `run_trio_parent` may hold it at a time; (b) on scaffold parent's BUILDING_DOMAINS entry, only dispatch ONE child at a time and wait for `task_finished` event before dispatching the next.

18. **Coders produce no diff** — every coder agent fired during the brief active window produced `coder_produced_no_diff` (rounds 1 and 2 visible in logs). After 3 attempts the dispatcher transitions the parent to BLOCKED (which is what happened to task 13). **Root cause unknown.** Diagnostic next-steps: (a) inspect the actual coder prompt being sent — see `agent/lifecycle/trio/dispatcher.py::dispatch_item` and follow into the coder agent. The coder is running under `claude_cli` provider — the stdin fix is in place, so this isn't ARG_MAX again. (b) check what files the coder is seeing — design.md is stashed away by my workspace fix; the coder may have lost critical context. **This may be a regression from the stash-on-branch-switch fix.**

---

## 4. Files changed (uncommitted in `/Users/alanyeginchibayev/Documents/Github/auto-agent-graph-fix` on `main`)

### Modified
- `agent/lifecycle/scaffold/parent.py` — freeform PO-standin gates + retry helper + notify-user
- `agent/lifecycle/scaffold/prompts.py` — scope contract on INTENT_GRILL_SYSTEM
- `agent/lifecycle/trio/__init__.py` — recursive re-entry after `architect.run_design`; repo-id fallback in `_try_freeform_design_standin`
- `agent/lifecycle/trio/recovery.py` — expanded filter (ARCHITECT_BACKLOG_EMIT, AWAITING_DESIGN_APPROVAL)
- `agent/llm/claude_cli.py` — prompt via stdin instead of argv
- `agent/workspace.py` — `create_branch` idempotency + stash-before-switch
- `orchestrator/create_repo.py` — `_first_prose_line()` helper; stamps `mode='freeform'` + `product_brief=description`
- `run.py` — wires `scaffold_heartbeat_watchdog` into lifespan
- `scripts/deploy.sh` — excludes `.claude/worktrees`, `.claude/scheduled_tasks.lock`
- `shared/types.py` — `TaskData.subtasks: list[dict] | dict | None`
- `tests/test_create_repo_name_generation.py` — 3 new title-extraction tests
- `tests/test_scaffold_e2e.py` — updated expected end-state after freeform walk-through
- `web-next/types/api.ts` — regenerated from `shared/types.py`

### Added
- `agent/lifecycle/scaffold/recovery.py` — `resume_all_scaffold_parents` + `scaffold_heartbeat_watchdog`
- `migrations/versions/054_normalize_046_051_enum_uppercase.py` — uppercase enum variants
- `tests/test_claude_cli_stdin.py` — stdin-pipe contract
- `tests/test_scaffold_intent_prompt.py` — scope-contract regression tests

**Nothing is committed yet.** Test suite green (2213 pass, 4 pre-existing failures unrelated). Ruff clean on every changed file.

---

## 5. The harpoon brief (preserve this — it's the user's product spec)

Saved at:
- `/Users/alanyeginchibayev/Documents/Github/auto-agent/docs/handovers/harpoon-brief.md` (local, 29 KB)
- `/tmp/harpoon-brief.md` on the VM (same content)
- `repos.product_brief` rows 237 + 238 in the production DB (truncated to 28608 chars)
- `tasks.description` row 12 in the production DB (full)

The brief describes a GTM outbound automation service. Mock vendors (Clay, Salesforge, Litemail, Warmforge, Slack), full v1 data model (10+ tables), 15 locked architectural decisions, suggested 5-phase build order (which the new scope-contract prompt treats as a hint, not a scope limit).

---

## 6. Engineering specs produced (the actual valuable output of this run)

In `/workspaces/1/task-12/.auto-agent/adrs/` on the VM:

| File | Size | Domain |
|------|------|--------|
| `000-system.md` | 14 KB | System decomposition into 7 domains |
| `001-platform.md` | 13 KB | Platform & operator shell |
| `001-platform.grill.md` | 31 KB | Pre-ADR grill summary |
| `002-campaign-orchestrator.md` | 24 KB | Campaign state machine |
| `002-campaign-orchestrator.grill.md` | 35 KB | … |
| `003-enrichment.md` | 32 KB | Clay integration + lead capture |
| `003-enrichment.grill.md` | 48 KB | … |
| `004-drafting.md` | 56 KB | Sonnet copywriter + Haiku validator |
| `004-drafting.grill.md` | 83 KB | … |
| `005-sequencing.md` | 78 KB | Salesforge contact upsert + sequence assign |
| `005-sequencing.grill.md` | 83 KB | … |
| `006-replies-notifications.md` | 105 KB | Haiku reply classifier + Slack DM |
| `006-replies-notifications.grill.md` | 112 KB | … |
| `007-funnel.md` | 92 KB | Events SoR + experiment cuts |
| `007-funnel.grill.md` | 102 KB | … |

Total ~900 KB of generated engineering content. These ARE real, detailed, useful design documents. They reference the brief's locked architectural decisions, define module boundaries, list aggregates, document affected routes, identify integration points and integration TODOs. **A human engineer could implement harpoon from these.**

To extract them: `ssh azureuser@172.190.26.82 'cd ~/auto-agent && docker compose exec auto-agent tar czf - /workspaces/1/task-12/.auto-agent/' > harpoon-design-pack.tar.gz`

---

## 7. What the next agent should do (priority order)

### Priority 1: don't lose the work
- **Commit the uncommitted files.** Test suite is green, ruff is clean, and these fixes address real bugs that bit production. Suggested commit boundary: one commit per concern (12 commits) so each fix is reviewable. Or one big "feat(freeform): wire scaffold + trio PO standins, recovery, idempotency" commit if you prefer.
- **Extract the harpoon design pack** off the VM before any deeper cleanup. The user might iterate the brief and re-run; the existing designs are a useful baseline either way.

### Priority 2: pick a stance on the architecture
The "Build something new" flow as it exists today has cascading failure modes — each layer (intent grill, root architect, domain architect, dispatcher, builder) has hand-rolled freeform plumbing with no end-to-end test coverage. Two reasonable paths:

**(A) Patch the open items and ship.** Fix bug 17 (per-scaffold serializer) and bug 18 (coder no-diff) and you have a working freeform scaffold path. Estimate: 2–4 hours for bug 17 (lock infrastructure) + open-ended for bug 18 (depends on root cause).

**(B) Pull the freeform path back to a feature flag and shore up the testbed.** Add an e2e test that exercises the FULL scaffold flow with mocked LLM providers (no real claude_cli) so the next regression isn't found by a 12-hour live run. Slow but defensible.

### Priority 3: open issues to investigate

- **Bug 18 (coder no-diff)**: most likely culprit is my workspace-stash fix (bug 13). The coder may be reading from a workspace where the design.md was just stashed away. Verify by running a coder agent on a fresh workspace and checking what files it sees.
- **Bug 17 (parallel coding)**: pick a serializer model. Per-scaffold lock is the user's stated contract. Trio dispatch should also probably honour a global concurrency cap; currently it doesn't.
- **Bug 12 (architect produces meta-only backlog)**: hard to reproduce without re-running. Worth adding an architect-output validator that rejects backlogs where every item is a `.auto-agent/`-touching artefact. Cheap heuristic.

### Priority 4: small wins to land first

- Migration 054 IS already deployed (no follow-up needed there, just commit it).
- The trio recovery expansion is load-tested by this run (it correctly resumed 7 trios with the design.md / has_backlog branch). Safe to commit.
- The claude_cli stdin fix is regression-tested. Safe to commit.
- The create_repo `mode='freeform'` + `product_brief` stamping is regression-tested. Safe to commit.

---

## 8. State of the production VM

- All 4 containers running (`auto-agent-{auto-agent,web-next,postgres,redis}-1`).
- Tasks 12–19 BLOCKED with `error='Manually halted 2026-05-23 — see docs/handovers/harpoon-run-2026-05-23.md'`.
- `repos` 237 + 238 still present (harpoon full + short alias). Mode `freeform`. Product brief populated.
- `freeform_configs` row 8 (or similar) attached to repo 238.
- GitHub `ergodic-ai/harpoon` exists with main branch + per-domain branches. No code, just architect commits with design.md sidecars on each child branch.

The next agent can decide whether to wipe tasks 12–19 + the repos before re-running, or keep them as evidence. If wiping, the SQL pattern from earlier in the run (cleanup before retry) handles all FK dependencies — see message history or replicate:

```sql
DELETE FROM suggestions WHERE repo_id IN (237,238);
DELETE FROM market_briefs WHERE repo_id IN (237,238);
DELETE FROM repo_graphs WHERE repo_id IN (237,238);
DELETE FROM repo_graph_configs WHERE repo_id IN (237,238);
DELETE FROM repo_secrets WHERE repo_id IN (237,238);
DELETE FROM tasks WHERE id BETWEEN 12 AND 19;
DELETE FROM freeform_configs WHERE repo_id IN (237,238);
DELETE FROM repos WHERE id IN (237,238);
```

And `rm -rf /workspaces/1/task-{12..19}` inside the auto-agent container.

---

## 9. What the user values + how they work

- **Forward progress over polish.** The user has been patient through 12+ hours of cascading bugs. They prefer "ship a working fix" over "perfect refactor", but they will catch when scope creep happens.
- **Trust their architectural calls.** When they said "if coding starts in parallel, that's not correct functionality" — that's a closed decision. Don't argue.
- **They don't want me to invent priors.** When uncertain, say "I lean toward X because Y", not "the conventional approach is X".
- **VM, not local.** Validation always targets the Azure VM. Don't try to fix local docker compose.
- **They'll re-authorize destructive actions if asked.** The classifier blocks DB DELETEs / GitHub deletes / mass UPDATEs even when the user gave intent via `AskUserQuestion`. Re-ask explicitly with the exact SQL and they'll click through.

---

## 10. The monitor I left behind

Stopped already (`TaskStop bsx7p2afs`). Pattern was: poll the production DB every 2 min, alert on (a) any `active_count > 1` (parallel coding indicator — turned out to be the wrong filter, see bug 17), (b) parent status transitions, (c) children-done count changes. If re-armed for a future run, also count tasks whose `parent_task_id` chains up to a SCAFFOLD root and watch `dispatcher.dispatch_item` invocations directly via log filter — that's what catches concurrency inside the per-item loop.

---

## Final note

This session ended with the user explicitly saying "stop all tasks, write a handover for the next agent". The handover is this file. Tasks are halted. Code state is uncommitted-but-tested. Recovery infrastructure is live and quiescent because no SCAFFOLD parent is in an active status anymore. The harpoon brief and the 900 KB of generated design documents are intact on the VM. The next agent should pick up at section 7 above.

— Claude, 2026-05-23
