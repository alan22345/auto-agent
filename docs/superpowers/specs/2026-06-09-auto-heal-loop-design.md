# Auto-Heal Loop — Design Spec

**Date:** 2026-06-09
**Status:** Draft (brainstorming output, pre-implementation)
**Branch context:** `feat/code-graph-health-tab`
**Prerequisite (done):** fail-closed self-review gates — commit `52964d6`
(see team-memory entity "Auto-Agent self-review verify gates").

## Goal

The code-graph **health tab** surfaces findings (dead code, clones, import
cycles, churn hotspots, poor-maintainability files) but offers no way to act
on them. Build an **autonomous, continuously-running loop** that drains those
findings indefinitely — filing, fixing, verifying, and staging each one —
without regressing functionality and without saturating the VM.

Hard constraint from the user: *"CI is not enough, the code should be run and
the UI verified."* Behavior must be provably preserved, not assumed.

## Decisions (locked during brainstorming)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Trigger model | Autonomous daemon — runs indefinitely; drains all finding categories |
| 2 | Concurrency | **Serial** — exactly one fix *in flight* at a time (drives VM memory; protected by the lease) |
| 2b | Batch size | A fix addresses **up to N findings** (`batch_size`, default 5, per-repo configurable). Independent of concurrency — still one agent / one branch / one verification run, so it adds no VM load |
| 3 | Merge target | A long-lived **cleanup branch**, always rebased onto `main`; never auto-merge to `main` |
| 4 | Regression guard | **Differential** (before/after) verification, in v1 |
| 5 | Verification | Three gates, all must pass: CI green + fail-closed smoke + differential |
| 6 | Scope | Per-repo, toggled from the health tab |
| 7 | Runtime model | The loop **is a task**; while active it holds an **exclusive scheduling lease** that hard-blocks all other task dispatch |
| 8 | Control | **Stop / Resume**. Stop releases the lease at the next safe checkpoint; to run your own work you Stop the loop |

## Non-goals (v1)

- **SPA visual diffing for un-routable SPAs.** Differential verification covers
  routes the smoke/route layer can reach + screenshot. Standalone Vite/React
  SPAs whose routes can't be inferred remain a known gap (carried over from the
  fail-open bug note); not closed here.
- **Cross-repo / global loop.** One loop per repo.
- **Auto-merge to `main`.** The loop only ever writes to the cleanup branch.
- **Parallel fixes / worktree isolation.** Serial only.

---

## Architecture

Eight components, each with one job and a narrow interface. Names are
provisional.

```
                       ┌─────────────────────────────────────────┐
   graph refresh ────► │  HealthLoop supervisor (a Task)          │
   (merge to main)     │  states: RUNNING · PAUSED · IDLE         │
   + idle poll         │  holds the exclusive scheduling lease    │
                       └───────────────┬─────────────────────────┘
                                       │ serial: one at a time
                  ┌────────────────────▼────────────────────┐
                  │ 1. HealthRanker → ordered findings       │
                  │ 2. Suppression filter                    │
                  │ 3. Findings→Task (batch up to N)         │
                  │ 4. Health-fix task (coder works all N    │
                  │    on one child branch off cleanup tip)  │
                  │ 5. Three-gate verifier                   │
                  │      a. CI green                          │
                  │      b. smoke (fail-closed — built)       │
                  │      c. differential (new)                │
                  │ 6. CleanupBranchManager (rebase/merge)   │
                  └──────────────────────────────────────────┘
```

### 1. HealthRanker
- **Input:** latest `RepoGraphBlob` for the repo (`GET /repos/{id}/graph/latest`
  data, already computed).
- **Output:** an ordered list of `HealthFinding` records, worst-first.
- **Ordering:** by the composite-health sub-score weighting already in
  `agent/graph_analyzer/health.py` (maintainability 0.30, dead-code 0.25,
  duplication 0.20, coupling 0.15, cycles 0.10), so the loop spends effort where
  it moves the score most.
- **Finding identity** — a stable `finding_hash` so the same finding is never
  double-filed and a parked/suppressed one is never re-picked across
  re-analyses:
  - dead code → `hash("dead", kind, target)`
  - cycle → `hash("cycle", sorted(members))`
  - clone → `hash("clone", family_id or sorted(instance file:line spans))`
  - hotspot → `hash("hotspot", file)`
  - poor file → `hash("file", file)` (only `band == "poor"`)

### 2. Suppression filter
- A per-repo set of suppressed `finding_hash`es (false positives, intentionally
  complex hotspots). Findings whose hash is suppressed are skipped by the ranker.
- Set from the health tab (per-row "suppress" action). Without this the loop
  would churn forever on findings it can't or shouldn't fix.

### 3. Findings → Task translator (batched)
- Takes the **top `N` un-suppressed, not-in-flight findings** from the ranker
  (`N = batch_size`, default 5) and bundles them into **one** fix task.
- Reuses `agent/po_graph_findings.py` summarization to render an
  **evidence-cited** task description listing all N findings (node IDs,
  `file:line`, metrics) so the coder has precise, grounded context for each.
- Emits a `Task` with `source = FREEFORM`, `freeform_mode = True`, a
  `parent_task_id` pointing at the supervisor, and a **batch** identity:
  - `source_id = f"health:{repo_id}:batch:{batch_hash}"` where `batch_hash`
    derives from the sorted member `finding_hash`es.
  - The task records the **set** of member `finding_hash`es (a
    `health_finding_hashes` column / JSONB) so dedup and suppression operate on
    members, not the batch: a finding already in flight or suppressed is
    excluded from the next batch even if its siblings are picked.
- **Attribution caveat (decision 2b):** because the batch is *not* segregated by
  category or locality, a differential failure parks all N together (see gate 5c)
  and a regression can't be pinned to a single member. `batch_size` is tunable
  down (to 1) per repo if this bites on behavior-changing categories.

### 4. Health-fix execution
- A dedicated handler (not generic classification) so every health fix takes the
  same path and always hits all three gates.
- The coder works **all N batch findings** on a single short-lived child branch
  cut from the **current cleanup tip** (= latest `main` + previously accepted
  fixes).

### 5. Three-gate verifier (all must pass)
1. **CI green** — the repo's existing CI on the child branch.
2. **Smoke (fail-closed)** — `pr_reviewer._smoke_gate` / `run_smoke_agent`
   (already built): the change provably runs (boot+curl / tests / build /
   typecheck). Verdict is always `pass|fail`, never skipped.
3. **Differential (new)** — the regression guard:
   - Boot the **base** (cleanup tip, pre-fix): capture route responses
     (`exercise_routes`) + UI screenshots (`inspect_ui`) for the touched/known
     routes. This is the baseline.
   - Boot the **branch** (post-fix): re-capture the same surfaces.
   - **Diff.** Any observable change — different status, body shape, or a
     vision-LLM "these screenshots differ materially" — is a regression and
     **rejects** the fix. Health fixes are behavior-preserving by definition, so
     divergence is always a red flag.
   - Invariant this enforces: *the cleanup branch behaves identically to `main`,
     just cleaner* (held inductively — each fix preserves it).
- **Pass all three** → hand to CleanupBranchManager to merge.
  **Fail any** → park the **whole batch** task `BLOCKED` with the diff/reason
  attached; the loop records all N member findings as parked and **moves on**
  (no infinite retry). (A `batch_size` of 1 recovers per-finding attribution.)

### 6. CleanupBranchManager
- Owns the long-lived `auto-agent/health-cleanup` branch (name configurable).
- **On accepted fix:** merge/commit the child branch into the cleanup branch.
- **On `main` advancing:** rebase the cleanup branch onto the new `main` and
  force-push. The branch is automation-owned, so force-push is acceptable —
  but the safe git wrapper (`agent/tools/git.py`) currently blocks
  force-push/reset; this manager needs a **scoped exception** limited to the
  cleanup branch. (Documented as an explicit carve-out, not a general loosening.)
- The cleanup branch is surfaced as a standing PR-to-`main` that the human
  reviews and merges on their own cadence. The loop never merges to `main`.

### 7. HealthLoop supervisor (the task + the lease)
- Modeled as a **long-lived `Task`** (type/`source` `health_loop`), visible in
  the task list — mirrors the trio parent/serial-children pattern already in the
  codebase (one child writing code at a time).
- **States:** `RUNNING` (working through findings), `PAUSED` (stopped by user),
  `IDLE` (no eligible findings — waiting for the next graph refresh).
- **Exclusive scheduling lease:** while the supervisor is `RUNNING` *or* `IDLE`
  (i.e. "active"), it holds a **VM-global** mutex (a single Redis key
  `vm_exclusive_lease`, TTL-guarded). The orchestrator dispatcher refuses to
  start any other task while the lease is held — user tasks stay `QUEUED`. The
  lease is global, not per-repo: even though loops are configured per-repo, only
  one loop (across all repos) can be active at a time, since the constraint
  being protected is the shared VM's memory, not any one repo. A second repo's
  loop contends for the same lease and waits. This caps heavy concurrent agents
  at 1 (the loop's current child) and is what prevents VM memory starvation.
- **Wake triggers:** graph re-analysis after a merge to `main` (fresh findings)
  + a slow idle poll. When `IDLE` and a new finding appears, transition to
  `RUNNING`.

### 8. Stop / Resume
- **Stop:** signal the supervisor to pause. It completes the **current** fix to a
  terminal state (merged-to-cleanup or parked) so nothing is left half-applied,
  then → `PAUSED` and **releases the lease**. Normal task dispatch resumes.
  (Optional later: a force-stop that aborts the in-flight fix immediately.)
- **Resume:** re-acquire the lease, → `RUNNING`, continue with the next worst
  finding.

---

## Data model

- **`HealthLoopConfig`** (new, or extend `RepoGraphConfig`):
  `repo_id`, `enabled: bool`, `cleanup_branch: str = "auto-agent/health-cleanup"`,
  `batch_size: int = 5`, `suppressed_finding_hashes: list[str]`, `state: enum`,
  `last_run_at`, `supervisor_task_id: int | None`.
- **`Task`**: reuse existing columns plus one addition.
  `source_id = "health:{repo}:batch:{batch_hash}"` is the batch dedup key;
  `parent_task_id` links fixes to the supervisor; a new
  `health_finding_hashes` JSONB column records the **member** finding hashes so
  dedup and suppression operate per-member (not per-batch).
- **Scheduling lease:** a single **VM-global** mutex — one Redis key
  `vm_exclusive_lease` (TTL-guarded so a crashed supervisor can't wedge the VM
  forever). Not per-repo (see component 7).

## API (orchestrator/router.py)

- `POST /repos/{repo_id}/health-loop/start` → create/enable supervisor, acquire lease.
- `POST /repos/{repo_id}/health-loop/stop` → graceful pause.
- `POST /repos/{repo_id}/health-loop/resume` → resume.
- `POST /repos/{repo_id}/health-loop/suppress` `{finding_hash}` → add to suppression set.
- `GET  /repos/{repo_id}/health-loop` → status (state, in-flight finding, cleanup
  branch + PR link, counts: fixed / parked / suppressed / remaining).

## UI (web-next, health tab)

- **Auto-heal toggle** + **Stop/Resume** control.
- **Status strip:** current state, in-flight finding, link to the cleanup branch
  / its open PR-to-`main`, counts (merged, parked, suppressed, remaining).
- **Per-row Suppress** action on each finding table.

## Reuses vs. builds new

- **Reuses:** freeform continuous-improvement loop, trio parent/serial-children
  pattern, coder, the fail-closed gates (`_smoke_gate`), `po_graph_findings`
  summarization, `exercise_routes` / `inspect_ui`, the health blob + ranker
  inputs, the existing concurrency/queue dispatcher.
- **Builds new:** HealthRanker + finding-hash, batch assembly (up to N members) +
  per-member dedup/suppression, the supervisor task + exclusive lease +
  Stop/Resume, the differential verifier, the CleanupBranchManager (rebase/merge
  + scoped force-push exception), the UI toggle/status/suppress.

## Failure & edge handling

- **A fix fails a gate** → park `BLOCKED`, attach reason, move on. Surfaced in
  the status strip's "parked" count; a human can inspect.
- **Rebase conflict** on the cleanup branch when `main` advances → park the
  conflict for human resolution; loop keeps working off the last clean tip if
  possible, otherwise goes `IDLE` until resolved.
- **Supervisor crash** → lease TTL expires so the VM isn't wedged; supervisor is
  restartable and resumes from the next worst un-suppressed finding (state is
  derived from findings + cleanup branch, not held in memory).
- **No eligible findings** → `IDLE`, lease still held (loop is "active" and
  blocking by decision #7/#8) until the user Stops.

## Acceptance criteria

1. With the loop enabled, fresh health findings result in a fix branch (a batch
   of up to `batch_size` findings) that is merged into the cleanup branch
   **only** after CI + smoke + differential all pass; a batch that changes
   observable behavior is rejected (parked as a whole), never merged.
2. The cleanup branch is never `main`; `main` is only ever changed by a human
   merging the cleanup PR.
3. While the loop is active, no other task is dispatched (verified: a submitted
   task stays `QUEUED` until Stop).
4. Stop releases the lease after the in-flight fix reaches a terminal state;
   Resume continues from the next worst finding.
5. A suppressed finding is never included in a batch.
6. The same finding is never double-filed across re-analyses or bundled into two
   concurrent batches (dedup by member `finding_hash`).
7. The loop runs indefinitely: it idles when findings are exhausted and wakes on
   the next graph refresh.

## Open questions (to resolve in planning)

- Exact home of `HealthLoopConfig` (new table vs. extend `RepoGraphConfig`).
- Differential screenshot comparison: pixel/structural diff vs. vision-LLM
  "materially different?" judgment (lean vision-LLM for robustness to
  antialiasing/timestamps).
- Whether the supervisor is a real `Task` row or a dedicated `HealthLoopRun`
  model that merely *appears* in the task list.
- Scope of the git force-push carve-out (branch-name allowlist).
```

