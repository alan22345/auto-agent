# HANDOVER — Auto-Heal Loop (code-graph health remediation)

**Date:** 2026-06-09
**Branch:** `feat/code-graph-health-tab` (pushed to origin; HEAD `73336bb`)
**Status:** Foundation + persistence layer COMPLETE, tested, and reviewed. Runtime glue + UI NOT started.
**Pick up at:** Phase 5b (supervisor loop) → 5c (batch handler) → 6 (API + web-next UI), build-deploy-verify against the VM.

---

## 1. What this is

An autonomous loop that drains code-graph **health findings** (dead code, clones,
import cycles, hotspots, poor-maintainability files) by filing+fixing+verifying+staging
each batch onto a long-lived cleanup branch — **without regressing functionality** and
**without saturating the VM**.

**Read first:** the design spec — `docs/superpowers/specs/2026-06-09-auto-heal-loop-design.md`.
It has the full architecture, the locked decisions, non-goals, and acceptance criteria.

### Locked decisions (do not relitigate without the user)
- **Serial** — one fix in flight at a time (VM-memory protection via the lease).
- **Batch up to N findings per fix** (`batch_size`, default 5, per-repo configurable).
- **Stage onto a cleanup branch** kept rebased on `main`; **never auto-merge to `main`** (a human merges the cleanup PR).
- **Three-gate verification per fix:** CI green + fail-closed smoke + **differential** (before/after).
- **Loop runs as a task** holding a **VM-global exclusive Redis lease** that hard-blocks all OTHER task dispatch. Stop/Resume; to run your own work you Stop the loop.
- **Per-repo**, toggled from the code-graph health tab.

---

## 2. What is DONE (complete, tested, two-stage-reviewed)

All modules live in `agent/health_loop/`. Every one was built TDD-first, then reviewed for
spec-compliance AND code-quality, with fixes applied. Full suite at handover: **2747 passed**,
105 skipped, **5 failed** — the 5 are PRE-EXISTING and unrelated (Playwright chromium not
installed, Slack-OAuth env, gate-notifications, graph-refresh-handler, slack-multi-team);
they fail identically on a clean tree. Confirm this baseline before blaming new work.

| # | Unit | Files | What it does |
|---|------|-------|--------------|
| prereq | **fail-closed verify gates** | `agent/lifecycle/pr_reviewer.py`, `agent/lifecycle/review.py`, tests `test_pr_reviewer.py`, `test_review_smoke_escalation.py`, `test_review_attempts.py` | Closed 3 fail-open self-review gates: when route-inference finds no routes, escalate to the fail-closed smoke agent instead of auto-approving. Single owner: `pr_reviewer._smoke_gate`. (commit `52964d6`) |
| 1 | **findings** (ranker) | `findings.py`, `test_health_loop_findings.py` | `HealthFinding`, `finding_hash` (stable, order-independent, category-prefixed), `extract_findings`, `rank_findings` (worst-first by composite-health weighting), `select_batch` (top-N minus suppressed/in-flight). PURE. |
| 2 | **differential** | `differential.py`, `test_health_loop_differential.py` | `differential_verify(base_workspace, branch_workspace, routes)` — boots both via `verify_primitives`, exercises routes, diffs **status + `ok` verdict + body** (JSON-aware), flags boot-state divergence, always tears down. Regression guard. |
| 3 | **cleanup_branch** | `cleanup_branch.py`, `test_health_loop_cleanup_branch.py` | `ensure_cleanup_branch`, `merge_fix` (no-ff, abort+False on conflict), `rebase_onto_base` (abort+conflict on conflict), `_force_push_cleanup` (allowlist guard — the ONLY force-push path). REAL-git integration tests. |
| 4 | **lease + dispatcher gate** | `lease.py`, `orchestrator/queue.py`, `test_health_loop_lease.py`, `test_queue_health_lease.py`, `tests/conftest.py` (autouse `_free_health_lease`) | VM-global lease (`acquire` NX, holder-guarded `renew`/`release`, `lease_held`/`lease_holder`). Gate in `queue.py::can_start_task`/`next_eligible_task`: while held, only `health:`-prefixed tasks dispatch. **Fails OPEN** on Redis error (a blip must not wedge all dispatch). |
| 5a | **config model + migration + service** | `shared/models/core.py` (`HealthLoopConfig`), `shared/models/__init__.py`, `migrations/versions/055_health_loop_config.py`, `agent/health_loop/config_service.py`, `test_health_loop_config.py` | Per-repo settings (`enabled`, `cleanup_branch`, `batch_size`, `state` idle/running/paused, `suppressed_finding_hashes` JSONB, `supervisor_task_id`, `last_run_at`). Async service: `get_config`, `get_or_create_config`, `set_enabled`, `set_state`, `suppress_finding`, `get_suppressed`, `_dedup_append`. |

**Commit range:** `52964d6` … `73336bb` (≈25 commits; see `git log --oneline 657c7b7..HEAD`).
The `docs/superpowers/plans/2026-06-09-auto-heal-loop*.md` files are the per-phase bite-sized plans.

---

## 3. THE VERIFICATION BOUNDARY (where "done" ends and "how we test" changes)

**Everything in §2 is LOCALLY verifiable** — pure logic, real-git temp repos, fake-redis,
or structural/import tests — and IS verified (tests green, reviewed).

**EXCEPTION — DB layer (5a):** locally `DATABASE_URL` is unset, so the config-service CRUD
**cannot run locally** (and the model uses Postgres `JSONB`, so SQLite can't substitute). What
IS verified locally for 5a: model columns/PK/tablename (pure), migration revision-chain + import
(pure), and the pure `_dedup_append`. **The CRUD behavior and the migration applying cleanly are
verified on the VM/CI** — run `alembic upgrade head` on the VM and exercise the service against
real Postgres. Do NOT trust 5a's CRUD until it has run against the VM.

**Everything in §4 (what's left) is INTEGRATION GLUE that is NOT meaningfully unit-testable.**
Exercising the supervisor/batch-handler means mocking the coder, the three gates, Redis, and the
DB — which tests the mocks, not the behavior. **These are verified by build-deploy-verify on the
VM** (see §6), not by local green tests. Resist the temptation to manufacture green via mocks; a
design-reviewed implementation + a real VM run is the honest signal here.

---

## 4. What is LEFT (not started)

### Phase 5b — Supervisor loop  `agent/health_loop/supervisor.py` (new)
A long-lived background async loop, modeled on the EXISTING precedent
`agent/po_analyzer.py::run_po_analysis_loop` (started in `run.py`). Responsibilities:
1. On enable/start: `acquire_lease(holder=f"health-loop:{repo_id}", ttl=…)`, set config `state="running"`.
2. Wake on the `repo.graph_ready` event (`shared/events.py::RepoEventType.GRAPH_READY`) AND a slow idle tick.
3. Load latest blob (`agent/po_graph_findings.py::load_latest_graph_blob(repo_id)`), `select_batch(blob, suppressed=get_suppressed(repo_id), in_flight=…, batch_size=cfg.batch_size)`.
4. Hand the batch to the **batch handler** (5c); await terminal (merged / parked).
5. **Renew the lease periodically** while a fix runs (fixes can exceed the TTL — renewal cadence is the supervisor's job; the primitive supports it via `renew_lease`).
6. When no eligible findings: `state="idle"`, keep the lease (the loop is "active" and blocking by decision), wait for the next wake.
7. **Stop:** finish the in-flight fix to a terminal state, then `release_lease`, `state="paused"`. **Resume:** re-acquire, `state="running"`.
8. Crash recovery: lease TTL expires so the VM isn't wedged; supervisor is restartable (state derived from config + cleanup branch).

### Phase 5c — Batch handler  `agent/health_loop/batch_handler.py` (new) — DEEPEST INTEGRATION
Given `(repo, list[HealthFinding])`, run ONE fix cycle:
1. Render an evidence-cited task description via `agent/po_graph_findings.py::summarize_graph_findings` (or a focused variant) listing the N findings.
2. File a `Task` with `source=FREEFORM`, `freeform_mode=True`, `source_id=f"health:{repo_id}:batch:{batch_hash}"`, `parent_task_id=<supervisor task>`, and store member hashes in the new `health_finding_hashes` column (NOTE: this column is in the spec but NOT yet added — needs a follow-up migration OR reuse an existing JSONB field; decide during 5c).
3. The fix runs on a child branch off the **current cleanup tip** (`cleanup_branch.ensure_cleanup_branch` then branch).
4. **Three gates (all must pass):** CI green (existing CI) + smoke (`pr_reviewer._smoke_gate` / `agent/lifecycle/trio/smoke_agent.run_smoke_agent`) + **differential** (`differential.differential_verify` with base=cleanup-tip, branch=fix-branch).
5. Pass → `cleanup_branch.merge_fix`. Fail any → park the task BLOCKED with reason; loop moves on (no infinite retry).
- **Hard part:** wiring the existing coder. Investigate how the coding lifecycle is invoked programmatically (`agent/lifecycle/coding.py`, `run.py` `on_task_*` handlers, the queue dispatch). The fix task likely flows through normal dispatch (it's a real coding task that the lease gate ALLOWS because of its `health:` source_id), and the batch handler watches it to terminal, then runs differential + merge. Decide: does the handler drive the coder directly, or file a task and observe it? Filing a task reuses the whole pipeline (recommended) — the handler then hooks the terminal transition to run differential + merge_fix.

### Phase 6 — API + web-next UI
- **API** (`orchestrator/router.py`): `POST /repos/{id}/health-loop/{start,stop,resume,suppress}`, `GET /repos/{id}/health-loop` (status: state, in-flight finding, cleanup-branch/PR link, counts merged/parked/suppressed/remaining).
- **UI** (`web-next/`, code-graph health tab): "Auto-heal" toggle + Stop/Resume, a status strip, and a per-row "suppress" action. Uses the Ergodic design system (`ergodic-ui` MCP — `init`/`list_components`/`get_component`); token classes only. Health tab is at `web-next/components/code-graph/health-tab.tsx`; page `web-next/app/(app)/code-graph/[repoId]/page.tsx`.

---

## 5. Grounded integration hooks (already mapped — saves the next agent the dig)

- **Dispatch concurrency / the gate:** `orchestrator/queue.py::can_start_task` (line ~73) and `next_eligible_task` (line ~87). Gate already wired; `is_health_loop_task` matches `source_id` `"health-loop"` or `health:`-prefix. Cap is `settings.max_concurrent_workers`.
- **Redis:** `shared/redis_client.py::get_redis()` → `aioredis` client, `decode_responses=False` (values are bytes). Lease key: `lease.HEALTH_LEASE_KEY = "auto-agent:health-loop:lease"`.
- **Wake signal:** `shared/events.py` — `RepoEventType.GRAPH_READY` (`"repo.graph_ready"`), payload `{repo_id, repo_graph_id, commit_sha, status}`. Published by `agent/lifecycle/graph_refresh.py`. Subscribe in `run.py` lifespan via the event bus (`bus.on("repo.graph_ready", …)`).
- **Background-loop precedent:** `agent/po_analyzer.py::run_po_analysis_loop`, started in `run.py` (search `run_po_analysis_loop`). Model the supervisor on it.
- **Graph blob loader:** `agent/po_graph_findings.py::load_latest_graph_blob(repo_id) -> RepoGraphBlob | None`. Finding summariser: same module, `summarize_graph_findings(blob)`.
- **Task model + creation:** `shared/models/core.py::Task`. Creation flows through `run.py::on_task_created` → `on_task_classified` → queue dispatch. `TaskSource` enum lives in `shared/models/core.py`.
- **Smoke gate:** `agent/lifecycle/trio/smoke_agent.py::run_smoke_agent(...)` (verdict always `pass`/`fail`). Also wrapped as `pr_reviewer._smoke_gate`.
- **Migrations:** `migrations/versions/`, integer revision ids; current head is **`055`** (this work). Template: `migrations/versions/053_repo_graph_flow_json.py`. Deploy runs `alembic upgrade head`.
- **Config DB style precedent:** `agent/lifecycle/graph_refresh.py::_load_config` (the async-session accessor pattern the config service mirrors).

---

## 6. HOW WE TEST THE REMAINING WORK (the plan)

Local unit tests stop being the signal at the integration layer. The agreed approach is
**build-deploy-verify against the VM**, in small increments:

1. **Build a piece** (e.g. the batch handler) with design-focused review (spec + quality
   subagents reviewing by inspection, since behavior can't be unit-run).
2. **Deploy:** `./scripts/deploy.sh` — rsyncs the working tree to `azureuser@172.190.26.82`,
   builds the image, runs `alembic upgrade head` (this is where migration `055` and any new
   migration actually apply), restarts `auto-agent` + `web-next`, health-checks
   `http://localhost:2020/health`. **The USER runs this** (it's their prod VM + SSH). Suggest
   `! ./scripts/deploy.sh` so output lands in-session.
3. **Verify the REAL check:** enable the loop on ONE repo, trigger a graph refresh
   (`POST /repos/{id}/graph/refresh`), and watch it actually:
   - acquire the lease (other tasks should queue, not run),
   - file a `health:` fix task,
   - land a fix on `auto-agent/health-cleanup` only after CI + smoke + differential pass,
   - leave `main` untouched.
   Check the cleanup branch on GitHub + the task list + logs. THAT is the verification — not a
   local mock.
4. Iterate. Tail VM logs: `ssh azureuser@172.190.26.82 "cd ~/auto-agent && docker compose logs -f auto-agent"`.

**First deploy gotcha:** the foundation (§2) is currently DORMANT in prod — nothing acquires
the lease, so the gate is a no-op (lease never held → fail-open returns "free" → normal
dispatch). Migration `055` will apply but no code reads `HealthLoopConfig` yet. So the first
deploy that includes runtime code (5b/5c) is the first one with a real behavior change — watch
it closely.

---

## 7. Known deferrals / gotchas (carry these forward)

1. **`is_health_loop_task` trusts a caller-influenceable `source_id` prefix** (`health:`). Today no namespace collides, but a crafted `source_id` could bypass the lease. **Fix in 5b/5c:** add a server-owned marker — a dedicated `TaskSource` enum value (e.g. `HEALTH_LOOP`) set at task creation, or a boolean column — matched by the gate instead of the string prefix. Needs a migration (bundle with 5c's `health_finding_hashes` migration). Reserve the `health:` namespace at task intake meanwhile.
2. **`health_finding_hashes` Task column** (member hashes for batch dedup/suppression) is in the spec but NOT yet added — add it in 5c (migration), or reuse an existing JSONB field.
3. **Differential is route-only.** UI screenshot diffing (Phase 2b) is deferred — `inspect_ui` doesn't expose a comparable artifact. A behavior-preserving fix to an un-routable SPA won't be visually diffed. Carry-over from the original fail-open bug note `[[bug_simple_flow_ui_gate_fail_open]]` (runtime fail-open closed; SPA-visual gap still open).
4. **Lease `renew_lease` TOCTOU** (documented in `lease.py`): non-atomic GET-then-SET without NX; safe under the serial single-supervisor design. If the loop ever becomes concurrent, replace with a Lua compare-and-set. The supervisor MUST renew well inside the TTL.
5. **`get_or_create_config` concurrent-create race** — benign for a serial loop (PK constraint; loser errors). Add `try/except IntegrityError → re-select` only if it ever runs concurrently.
6. **Commit authorship:** this session's commits are authored `alanyeginchibayev@Alans-MacBook-Pro.local` (local git default), no `Co-Authored-By` trailer. Cosmetic; left as-is on the user's call. Clean up before the eventual PR if desired.
7. **`.claude/worktrees/`** is untracked and excluded by `deploy.sh` — never `git add -A` (it would sweep embedded git repos in). Always commit with explicit paths.

---

## 8. Quick-start checklist for the next agent

- [ ] `git status` on `feat/code-graph-health-tab`; confirm HEAD `73336bb` (or later).
- [ ] Run `.venv/bin/python3 -m pytest tests/ -q` — confirm the 5-failure pre-existing baseline (no NEW failures).
- [ ] Read `docs/superpowers/specs/2026-06-09-auto-heal-loop-design.md` end to end.
- [ ] Read this handover §4–§7.
- [ ] Recall team-memory entity **"Auto-Agent code-graph health auto-heal loop"** for the running context.
- [ ] Start Phase 5b (supervisor) — author a plan grounded in `run_po_analysis_loop`, then build, then deploy+verify per §6. Decide the 5c coder-wiring approach (recommended: file a `health:` task, observe it to terminal, then run differential + `merge_fix`).
```
