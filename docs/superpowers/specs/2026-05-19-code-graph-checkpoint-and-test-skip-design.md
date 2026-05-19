# Code-graph resumable analysis + test-file exclusion

**Date:** 2026-05-19
**Status:** Approved (brainstorm complete; awaiting user review of spec; then writing-plans)
**Related ADRs:** ADR-016 (code graph)

## Context

ADR-016 (code graph) is live on origin/main, but the analyser today is non-resumable:

1. `run_pipeline` builds an entire `RepoGraphBlob` in memory and only writes a `repo_graphs` row when the whole pipeline completes.
2. A crash, container restart, rate-limit pause, or any other interruption discards every LLM call (gap-fill + agent-escape) made so far.
3. On the next refresh, the pipeline starts from scratch — the workspace clone is preserved on disk but the LLM work is fully redone.
4. There is also no per-commit incremental path: re-analyzing a single file change today means re-running gap-fill across every file in the repo.

Two other operational facts surfaced today during the first real run against `cardamon`:

- **Test files dominate the gap-fill cost.** Test/fixture/mock files (`__tests__/`, `*.test.ts`, `**/__mocks__/`, `cypress/`, `e2e/`) account for a large fraction of the file walk yet rarely have real dispatch edges worth resolving. They burn budget without adding signal.
- **Without incremental support, even a small commit forces a full re-analysis.** This is unaffordable at cardamon's size (~721 source files; estimated 3–5 hours end-to-end).

## Goals

1. **Mid-flight resumability.** A run that dies after processing N of M files can be resumed by re-triggering Refresh; only ~1 file's worth of work is ever lost.
2. **Commit-diff incremental analysis.** When the branch's HEAD advances, only files that changed (and their statefully-implicated neighbours) get re-analyzed.
3. **No-op on unchanged commit.** Re-triggering Refresh when the row is complete and the commit is unchanged should return immediately with no LLM cost.
4. **Live partial graph in the UI.** The user sees the graph build up file-by-file and can browse the partial result with a clear "in progress" badge.
5. **Skip test files entirely.** Test/fixture/mock files are excluded from parsing so they never appear as nodes/edges and never consume LLM budget.

## Non-goals (v1)

- Per-repo override of the test-file pattern set. (Hardcoded v1; future config via `.auto-agent/graph.yml`.)
- A "force full re-analyze" UI button. (Operator can clear the row manually if needed.)
- Site-level resumability (we agreed on file-level granularity; finer adds disproportionate DB write churn).
- Capping the per-site retry counter. (Recorded in `failed_sites.attempts` but not gated on a max.)

## Locked decisions (from brainstorming)

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Mid-flight resumable, not just between-run incremental | User answer Q1 = B |
| 2 | Per-file checkpoint granularity | User answer Q2 = B; balance of write-cost vs work-loss-on-crash |
| 3 | On commit change: pull → diff → drop changed-file checkpoint entries → resume | User answer Q3 = A |
| 4 | Skip test files at file-walk level (no tree-sitter parse) | User answer Q4 = A |
| 5 | Retry every errored site on resume (incl. agent_escape bound-hits) | User answer Q5 = A |
| 6 | UI shows live partial graph + a "complete" badge | User answer Q6 = C |
| 7 | No-op-on-unchanged-commit re-uses `repo_graph_ready` event | User answer Q7 = A |
| 8 | Smart cascade on M files: only re-walk callers if a previously-targeted node was lost | User answer = C |

## Architecture

Two orthogonal changes layered on the current pipeline:

**Change A — test-file exclusion** (small, surgical). A new helper `is_test_file(rel_path)` filters the file walk before tree-sitter sees the file. Test files are absent from the graph entirely.

**Change B — mid-flight checkpointing + commit-diff resume** (the meaningful change). The pipeline holds a single `repo_graphs` row open across runs, UPDATEs after each file, and uses git diff to decide what to drop and re-process when the branch advances.

```
                ┌────────────────┐
  Refresh ───►  │ run_refresh    │
                │   (orchestrator)
                └───┬────────────┘
                    │
                    ▼
        ┌───────────────────────────┐
        │ acquire workspace lock    │
        │ clone or fetch + reset    │
        │ resolve HEAD commit_sha   │
        └───────────────┬───────────┘
                        │
                        ▼
        ┌───────────────────────────┐
        │ load OR create row        │  see "Row-load cases" below
        │ in repo_graphs            │
        └───────────────┬───────────┘
                        │
                        ▼
        ┌───────────────────────────┐
        │ if commit changed:        │
        │   diff old_sha → HEAD     │
        │   apply ChangedFilesPlan: │
        │     drop entries, prune   │
        │     nodes/edges, cascade  │
        └───────────────┬───────────┘
                        │
                        ▼
        ┌───────────────────────────┐
        │ walk repo;                │
        │ for each non-test file:   │
        │   if in processed_files   │
        │       AND no retry-due:   │  skip
        │   else:                   │
        │       parse + sites       │
        │       gap_fill / escape   │
        │       merge edges         │
        │       UPDATE row          │  checkpoint per file
        └───────────────┬───────────┘
                        │
                        ▼
        ┌───────────────────────────┐
        │ is_complete = true        │
        │ publish repo_graph_ready  │
        └───────────────────────────┘
```

### Row-load cases (in `run_refresh`)

| Existing row | `commit_sha` vs HEAD | Action |
|---|---|---|
| None | — | INSERT fresh row, `is_complete=false`, `commit_sha=HEAD`. Full analysis. |
| `is_complete=true` | `==` HEAD | **No-op.** Publish `repo_graph_ready` with the existing row id, release lock, return. |
| `is_complete=true` | `!=` HEAD | Flip `is_complete=false`, update `commit_sha=HEAD`. Diff old→HEAD. Apply ChangedFilesPlan. Resume walk. |
| `is_complete=false` | `==` HEAD | Resume in place. Walk unprocessed files. Retry `failed_sites`. |
| `is_complete=false` | `!=` HEAD | Same as the third row: diff, apply plan, continue. |

### Invariants

- **One row per repo, ever.** No new row on resume; `repo_graphs.id` is stable. Trade-off: we don't keep a history of past completed analyses — the row is mutated in place when a new run starts. Acceptable per the user's stated needs (current-state-only); a future "snapshot before re-analysis" feature would copy the row before mutating.
- **Test files are never in `processed_files` / nodes / edges.** Filtered at the walk.
- **All resume logic is in-process** at the start of `run_refresh`. No background scheduler.

## Schema

New migration `migrations/versions/048_repo_graph_checkpoint.py` adds three columns to `repo_graphs`:

```python
is_complete       BOOL    NOT NULL DEFAULT false
processed_files   JSONB   NOT NULL DEFAULT '{}'::jsonb
failed_sites      JSONB   NOT NULL DEFAULT '[]'::jsonb
```

Migration's data step sets `is_complete=true` for all existing rows: the old code only ever wrote rows on full completion, so historically `is_complete=true` is correct for them.

### `processed_files` shape

```json
{
  "agent/loop.py": {
    "sites_attempted": 12,
    "sites_succeeded": 9,
    "edges_added": 7,
    "processed_at": "2026-05-19T08:46:33Z"
  },
  "app/admin/page.tsx": { ... }
}
```

Keyed by repo-relative path. Used for: skip-on-resume detection, progress estimate, file-diff intersection on commit change.

### `failed_sites` shape

```json
[
  {
    "file": "app/foo.ts",
    "site_id": "app/foo.ts::handler",
    "kind": "gap_fill_provider_error",
    "error": "Error code: 429 - ...",
    "attempts": 1,
    "last_attempt_at": "2026-05-19T08:46:33Z"
  }
]
```

Retried on every resume per the locked decision (no per-attempt cap in v1; `attempts` is recorded for observability).

No change to existing columns (`graph_json`, `commit_sha`, `status`, `analyser_version`, etc.). The graph blob still lives in `graph_json` and is updated in place.

## Pipeline flow (modifications)

### `run_refresh` (orchestrator side, `agent/lifecycle/graph_refresh.py`)

1. Acquire workspace lock (existing).
2. `prepare_workspace` (existing — clone or fetch+reset).
3. Resolve HEAD `commit_sha`.
4. Load or create row per the Row-load cases table above.
5. If commit_sha changed since the row's `commit_sha`, call `diff.changed_files(workspace, old_sha, HEAD)` and apply the plan (see "Commit-diff handling" below).
6. Call `run_pipeline(...)` passing the row id + the in-memory blob loaded from `graph_json`.
7. After pipeline returns, mark `is_complete=true`, update `commit_sha`, set the final `status`, commit.
8. Publish `repo_graph_ready` (existing).
9. Release lock.

### `run_pipeline` (analyser side, `agent/graph_analyzer/pipeline.py`)

The file walk loop changes from "build blob, return" to "build blob incrementally, flush per file":

```python
for rel_path in walk_files(workspace):
    if is_test_file(rel_path):
        continue
    if rel_path in processed_files and not any_retry_due_in(rel_path, failed_sites):
        continue

    parse_result = parse_file(rel_path, ...)
    sites = extract_sites(parse_result)
    new_edges, errored_sites = await process_sites(sites, provider=...)

    blob.add_nodes(parse_result.nodes)
    blob.add_edges(new_edges)
    processed_files[rel_path] = {
        "sites_attempted": len(sites),
        "sites_succeeded": len(sites) - len(errored_sites),
        "edges_added": len(new_edges),
        "processed_at": now(),
    }
    failed_sites = [s for s in failed_sites if s["file"] != rel_path] + errored_sites
    await update_row(row_id, blob=blob, processed_files=processed_files, failed_sites=failed_sites)
```

### Crash semantics

If the worker dies between `process_sites` and `update_row`, only that one file's work is lost. Resume re-runs that file from scratch — per the per-file granularity contract.

If the worker dies after `update_row` but before the next file, the file is recorded as done. Standard.

## Commit-diff handling (`agent/graph_analyzer/diff.py`, new)

```python
def changed_files(workspace: str, from_sha: str, to_sha: str) -> ChangedFilesPlan:
    """Returns added, modified, deleted, renamed_pure, renamed_modified."""
```

Implementation uses:

```bash
git diff --name-status --diff-filter=AMRTUD -z <from_sha> <to_sha>
```

`-z` separates with NUL so paths with whitespace are safe. `--name-status` exposes the rename similarity score (`R100 old new` = pure rename; `R<100 old new` = rename + modify).

### Action per status

| Status | Action |
|---|---|
| `A path` | Walk + parse + gap_fill the new file. Add nodes/edges. No checkpoint pre-touch. |
| `M path` / `T path` | (1) Capture the set of nodes in this file that are targeted by edges whose source is in a **different** file: `cross_file_targets = {e.target.id for e in edges if e.target.file == path AND e.source.file != path}`. Self-edges within the modified file don't trigger cascade — the file is being re-walked anyway. (2) Drop `processed_files[path]`. (3) Drop nodes where `file=path`. (4) Drop edges where `source.file=path` OR `target.file=path`. (5) Re-walk and re-process. (6) Compute `still_lost = cross_file_targets - {n.id for n in new_nodes_in_path}`. (7) For every (just-dropped) edge whose `target.id in still_lost`, the corresponding `source.file` is added to a cascade re-walk set. (8) Drop checkpoint entries for cascaded files so they get re-walked in the same run. |
| `D path` | Drop `processed_files[path]`. Drop nodes where `file=path`. Drop edges where `source.file=path` OR `target.file=path`. **Cascade:** drop checkpoint entries for every file that had any edge with `target.file=path`. Those files get re-walked because their gap-fill calls may resolve to different nodes now that the previous target is gone. |
| `R100 old new` | Pure rename. Rewrite `file` field on all nodes from `old → new`. Rewrite edges where `source.file=old` or `target.file=old`. Move `processed_files[old] → processed_files[new]`. No re-walk. |
| `R<100 old new` | Rename + modify. Treat as `D old` + `A new`: drop old nodes/edges, cascade callers, walk new path. |

### Checkpoint commit no longer reachable

If `git diff <checkpoint_sha> HEAD` errors with "unknown revision" (force-push, branch tip rewrite, etc.), catch it and **fall back to full re-analysis from scratch**: clear `processed_files`, `failed_sites`, `graph_json`; set `commit_sha=HEAD`; walk every file. Log a `graph_refresh_checkpoint_unreachable` event so the operator knows checkpoint state was discarded.

### Test files in diff output

A test file path in the diff is simply ignored — no entry to drop, no node/edge to remove. The pipeline still filters test files at walk time so they'd never be picked up regardless.

## Test-file exclusion

`agent/graph_analyzer/pipeline.py` (or a sibling helper module):

```python
_TEST_DIR_NAMES = frozenset({
    "__tests__", "__mocks__",
    "tests", "test",
    "cypress", "e2e",
})
_TEST_FILE_RE = re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx|py)$")

def is_test_file(rel_path: str) -> bool:
    if _TEST_FILE_RE.search(rel_path):
        return True
    return any(part in _TEST_DIR_NAMES for part in rel_path.split("/"))
```

Called inside `walk_files`. Matched files are skipped before tree-sitter parse — no nodes, no edges, never recorded in `processed_files`.

### Edge cases acknowledged

- A file like `apps/test-utils/foo.ts` has `test-utils` as a path part — does NOT match. (The literal directory name `test-utils` is not in `_TEST_DIR_NAMES`.)
- A file like `lib/foo.test.tsx` matches the regex regardless of its directory.
- Symlinks into test dirs are out of scope; the walk already deduplicates by realpath.

## API + UI changes

### API (`orchestrator/router.py`)

- `GET /repos/{id}/graph/latest` — response gains `is_complete`, `processed_files_count`, `total_files_estimate`. The estimate comes from a 30s-cached file walk count (excluding tests).
- `POST /repos/{id}/graph/refresh` — unchanged signature.
- `GET /repos/{id}/graph/progress` (new) — lightweight: `{is_complete, processed: int, total: int, last_file: str | null, status: "running"|"idle"|"unchanged"}`. Used for polling without re-shipping the blob.

### UI (`web-next/app/(app)/code-graph/[repoId]/page.tsx`)

- `useRepoGraph()` keeps returning the full blob; the canvas renders whatever's present, even when partial.
- `useRepoGraphProgress(repoId)` (new) — TanStack-Query polling `/graph/progress` every 5s while `is_complete=false`, every 60s otherwise. Stops polling on `is_complete=true`.
- `<GraphCompletionBadge />` (new) — pill next to `<FreshnessBanner />`:
  - Green: "Complete · X files".
  - Amber + spinner: "Analyzing · X / Y files (file: app/foo.ts)".
  - Grey: "Partial · X / Y files. Click Refresh to resume."
- Existing Refresh button label flips between "Refresh" (complete) and "Resume / Re-analyze" (partial). Click POSTs to the same endpoint either way; server decides resume vs no-op.

### Events (`shared/events.py`)

No new event types. `repo_graph_requested` / `repo_graph_ready` / `repo_graph_failed` already cover every case. UI distinguishes "freshly analyzed" vs "no-op completion" by comparing `commit_sha` in the WS payload against its prior value.

## Error handling

| Failure | Behavior |
|---|---|
| Single-file parse error | Caught inside the file loop. The file ends up in `processed_files` with `sites_attempted=0, sites_succeeded=0, edges_added=0`. No retry (parse failures rarely self-heal). Logged as `graph_file_parse_failed`. |
| Single-site provider error (gap-fill or agent-escape) | Caught inside `process_sites`. Site is appended to `failed_sites`. Pipeline continues. Logged as today. |
| `update_row` DB write fails | Bubble up, fail the whole run, publish `repo_graph_failed` (today's behavior preserved). On resume the in-memory blob is rebuilt from the last successful DB state. |
| `git diff` against unreachable commit | Fall back to full re-analysis from scratch (see Commit-diff section). |
| Container restart mid-run | Lock released by process death. Next Refresh acquires lock, sees `is_complete=false`, resumes per the row-load cases table. |
| Two concurrent Refresh requests | Existing `graph_workspace_lock` rejects the second one with `repo_graph_failed(error="analysis already running")`. Unchanged. |

## Testing

### Unit

- `tests/test_graph_pipeline_test_filter.py` — `is_test_file` against ~20 positive + ~10 negative paths.
- `tests/test_graph_pipeline_diff.py` — `changed_files` against fabricated `git diff --name-status -z` outputs for A/M/T/D/R100/R<low>; plus the "unreachable commit" → fallback case.
- `tests/test_graph_pipeline_resume.py` — file-level checkpoint and smart-cascade logic against in-memory fixtures. Fresh run, resume on same commit, resume across commit with each diff status type, smart-cascade trigger when a previously-targeted node is lost in the new walk.

### DB-backed (skip if no DATABASE_URL)

- `tests/test_repo_graph_resume_db.py` — first run creates and finalizes a row; mid-flight failure leaves `is_complete=false`; same-commit re-trigger is a no-op (zero LLM, zero UPDATE); commit-change re-trigger applies the file-diff plan before the walk continues.

### End-to-end

- `tests/test_graph_refresh_resume_e2e.py` — uses the existing `_run_git` + `run_pipeline` mock patterns from `tests/test_graph_refresh_handler.py`. Start refresh, cancel mid-pipeline, restart refresh, assert the final blob equals what a full single-run would have produced.

### UI

- `web-next/tests/graph-completion-badge.test.tsx` — renders each of the three states.
- `web-next/tests/use-repo-graph-progress.test.ts` — polling cadence (5s vs 60s).

### Existing test updates

- `tests/test_graph_refresh_handler.py` — current tests assume a single INSERT; they need to assert UPDATE-per-file. While in there, the five `/data` macOS-path failures get their workspace path mocked properly.
- `tests/test_repo_graph_migration_033.py` — sibling test for migration 048.

### Eval impact

None — graph analysis isn't exercised by the agent eval suite.

## Out-of-band cleanup

This change is independent of, but related to:
- Today's stuck pipeline state on the VM (lock cleared by the kill, no checkpoint to honor since the schema doesn't exist yet — first run after this ships will be a clean full analysis).
- `repo_graphs.id=1` (the May 18 stub with `status=ok` from the pre-tree-sitter run). The migration's data step marks it `is_complete=true`. Its `graph_json` is mostly empty; a fresh refresh will overwrite via the no-op-on-unchanged-commit path only if `commit_sha` matches, otherwise apply diff. Practically the next Refresh will see commit divergence and re-walk most of the repo.
