# Workspace GC — Design

**Date:** 2026-05-30
**Status:** Approved (design); pending implementation
**Author:** Claude + Alan

## Problem

The production VM froze overnight (2026-05-29 → 05-30): the host became
resource-starved (no swap, ~7.7 GB RAM, root disk at 81%) under sustained
build load until sshd and the app stopped responding while the kernel kept the
network up. An Azure soft-reboot recovered it and the trio recovery sweep
resumed the build.

Disk pressure is a standing contributor. Agent workspaces accumulate under
`/workspaces` and are never fully reclaimed:

- Per-task cleanup **does** exist — `cleanup_workspace(task_id, organization_id)`
  (`agent/workspace.py`) fires on the `task.cleanup` event from
  `on_task_finished` (`run.py`). But it only removes `task-<id>` dirs, and only
  if the finish event actually fires — a host crash (like last night's) skips it,
  leaving orphaned terminal task dirs.
- The long-lived **system** workspace dirs — `arch-*`, `po-*`, `market-*`,
  `summary-*`, `harness-*`, `conflict-resolve-*` — are **never** deleted by any
  code. They are reused across runs (a `git fetch` + checkout into an existing
  clone) and simply grow, each carrying a full checkout plus `node_modules`.

## Goal

A periodic, age-gated background task that reclaims disk by deleting stale
workspace directories, so the host stops creeping toward the disk/OOM freeze.
It must never delete a directory in active use.

Non-goals (YAGNI): disk-threshold/pressure triggering, `node_modules` stripping,
compression/archival, cross-host coordination.

## Behavior

A background loop wakes **every 6 h** (configurable) and scans the workspace root
`WORKSPACES_DIR` (`/workspaces` in deploy; env-driven, read in
`agent/workspace.py`). For each immediate child directory it classifies and
decides whether to delete:

| Dir pattern | Delete when |
|---|---|
| `task-<id>` (flat, legacy) and `<org_id>/task-<id>` | task status is terminal (`DONE`/`FAILED`) **or** the task row no longer exists, **AND** age > `MAX_AGE` |
| System dirs: `arch-*`, `po-*`, `market-*`, `summary-*`, `harness-*`, `conflict-resolve-*` | age > `MAX_AGE` (reuse-by-re-clone: deletion just forces a fresh clone next run) |
| Anything else (`.pnpm-store`, the numeric `<org_id>` dirs themselves, unrecognized names) | **never deleted** |

Default `MAX_AGE` = **24 h**.

### Safety rules

- **Allow-list, not deny-list.** Only the two recognized shapes above are ever
  removed. Any unrecognized directory name is always skipped.
- **Active-task protection.** Task dirs are deleted only when the task is
  terminal. Terminal = `DONE` or `FAILED` only (per
  `orchestrator/state_machine.py`: `DONE` has no outgoing transitions, `FAILED →
  DONE`). All other ~20 statuses (INTAKE, CLASSIFYING, QUEUED, CODING,
  TRIO_EXECUTING, ARCHITECT_DESIGNING, the ADR-018 scaffold chain, BLOCKED,
  BLOCKED_ON_AUTH/QUOTA, …) are treated as active. We do **not** rely on
  `orchestrator/queue.py::ACTIVE_STATUSES` (it is queue-slot-scoped and omits
  many in-flight statuses).
- **Age = most-recent activity**, computed as
  `now - max(mtime(dir), mtime(dir/.git/FETCH_HEAD) if present)`. The
  `FETCH_HEAD` check catches a system dir reused via `git fetch` whose top-level
  mtime did not change, so we don't delete a repo that is actively being reused.
- **Path containment.** Every delete target is `os.path.realpath`-checked to be
  strictly inside the resolved `WORKSPACES_DIR` before any `shutil.rmtree`.
- **Org-dir recursion is one level only.** A numeric `<org_id>` dir is descended
  into to find `task-<id>` children; the org dir itself is never deleted.

## Structure & files

### New module: `agent/workspace_gc.py`

Keeps `agent/workspace.py` under the ~500-line guideline and isolates the GC as
an independently testable unit.

- `scan_reclaimable(root: str, now: float, max_age_seconds: float,
  terminal_task_ids: set[int]) -> list[tuple[str, str]]`
  Pure, filesystem-only decision logic. Returns `(path, reason)` candidates. No
  DB access, no deletion. `terminal_task_ids` is the set of task ids known to be
  terminal-or-missing (computed by the caller). Fully unit-testable against a
  temp dir.
- `async def run_workspace_gc(max_age_hours: float = MAX_AGE_HOURS) -> dict`
  Lists workspace dirs, parses `task-<id>` ids, queries the DB once for those
  ids' statuses, builds the terminal-or-missing set, calls `scan_reclaimable`,
  deletes each candidate (with path-containment check), and returns/logs a
  summary `{n_deleted, bytes_freed, duration_s}`.
- `async def workspace_gc_loop() -> None`
  `while True: try: run_workspace_gc(); except: log; await asyncio.sleep(interval)`
  wrapper, mirroring `task_timeout_watchdog`. Honors `WORKSPACE_GC_ENABLED`.

### `run.py`

Import `workspace_gc_loop` and add it to the `bg = [...]` background-task list in
the FastAPI lifespan (alongside `task_timeout_watchdog`, `pr_merge_poller`,
`_scaffold_heartbeat_runner`, etc.), so it starts on boot and is cancelled
cleanly on shutdown.

### Configuration (env, matching the `WORKSPACES_DIR` pattern)

- `WORKSPACE_GC_ENABLED` — default `true`. When false, the loop logs once and
  exits (feature flag / kill switch).
- `WORKSPACE_GC_MAX_AGE_HOURS` — default `24`.
- `WORKSPACE_GC_INTERVAL_HOURS` — default `6`.

## Observability

structlog events, no silent deletes:

- `workspace_gc.deleted` per dir: `{path, reason, age_hours, bytes_freed}`.
- `workspace_gc.sweep_complete`: `{n_deleted, bytes_freed, duration_s}`.
- `workspace_gc.skipped_unsafe` if a candidate fails the containment check
  (should never happen; logged at warning).

## Testing (TDD)

New `tests/test_workspace_gc.py`, driving `scan_reclaimable` against a temp dir
(no DB, deterministic `now`):

1. Terminal `task-1` (old) → deleted; active `task-2` (old) → kept; `task-3`
   with no DB row, i.e. id in `terminal_task_ids` as "missing" (old) → deleted.
2. Fresh terminal task dir (age < threshold) → kept.
3. Old `arch-foo` / `market-bar` → deleted; fresh `po-baz` → kept.
4. Unrecognized `.pnpm-store` (old) → never selected.
5. `<org_id>/task-<id>` nested layout handled; the org dir itself never selected.
6. Path-containment: a symlink whose target is outside root → never selected.
7. `FETCH_HEAD` recency: a system dir with an old top-level mtime but a fresh
   `.git/FETCH_HEAD` → kept.

Run the full suite + `ruff check .` before claiming done.

## Rollout

Implement on the `workspace-gc` branch, verify tests + lint, then deploy via
`scripts/deploy.sh`. The loop starts on the next container boot. First sweep
reclaims orphaned terminal task dirs and stale system dirs > 24 h old.
