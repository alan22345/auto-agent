# [ADR-012] LGTM-driven auto-merge and merge-conflict resolution

## Status

Accepted

## Context

Freeform-mode tasks today auto-merge only on `task.ci_passed`, and only if the
repo has a `FreeformConfig.dev_branch`. Two gaps surfaced when freeform PRs
piled up unmerged on the auto-agent repo itself (PRs #33/#36/#37):

1. **No LGTM signal.** A reviewer (human or agent) submitting a GitHub review
   with `state=approved` had no effect on the merge gate. The webhook handler
   (`orchestrator/webhooks/github.py:201`) just logged it. Freeform tasks that
   wanted "anyone-LGTM merges" had no path.
2. **No conflict handling.** When a PR became `mergeable_state: dirty` because
   `main` moved while the feature branch was open, auto-merge would either
   silently fail (current freeform path) or never get tried (LGTM path that
   didn't exist). The user had to manually rebase, push, and re-trigger.

We want freeform mode to look like the natural review-driven flow:
*review PR → comment → resolve comments → merge*. When the review concludes
"LGTM," auto-agent should be able to merge — including when the PR has drifted
and needs base-branch changes pulled in.

## Decision

Two cooperating pieces:

### 1. LGTM-driven auto-merge

- `pull_request_review` webhook with `state=approved` (any reviewer, no
  pattern matching on body) emits `task.lgtm_received`.
- `on_lgtm_received` (`run.py`) gates the merge:
  - **freeform-mode only.** Non-freeform tasks ignore LGTM and stay in
    AWAITING_REVIEW. The human-review safety path is unchanged.
  - **CI green-or-absent.** GitHub's `mergeable_state` is the authoritative
    signal; values `clean` and `has_hooks` proceed. `unstable` (failing CI)
    falls through to AWAITING_REVIEW.
  - **No conflicts.** `mergeable_state == "dirty"` triggers the resolver
    (below) and leaves the task in flight.
- On a successful merge, the task transitions to AWAITING_REVIEW → DONE
  (mirroring the existing CI-passed path).

LGTM **does not** require `FreeformConfig.dev_branch` — that gate only
applies to the headless "CI passed, no review needed" path. Reviewer-approved
freeform PRs can target any base.

### 2. Pre-emptive conflict resolution

`_auto_merge_pr` now checks `mergeable_state` before the merge API call. The
new outcomes are:

| Outcome | Meaning |
|---------|---------|
| `MERGED` | Squash-merge succeeded |
| `CONFLICT_DISPATCHED` | PR was `dirty`; resolver agent dispatched |
| `CI_BLOCKED` | PR was `unstable` (failing CI) |
| `FAILED` | Anything else (auth, blocked, etc.) |

When `dirty` is detected:

1. Set Redis key `task:{id}:conflict_resolution_attempted` (TTL 24h).
2. Emit `task.merge_conflict_detected` with `pr_url`.
3. Return `CONFLICT_DISPATCHED` — caller leaves the task in flight.

`agent/conflict_resolver.py` subscribes via `agent/main.py`'s event loop and:

1. Fetches `head` and `base` branches from GitHub.
2. Clones the head branch into a fresh workspace.
3. Runs `git fetch origin <base>` then `git merge --no-ff origin/<base>`.
4. **Clean merge** (no conflicts) → finalize the merge commit, push.
5. **Conflicts** → invoke the agent loop with a focused prompt. The agent has
   read/write access to any file (conflict resolution sometimes needs broad
   edits, e.g. updating imports after a base-branch rename). The agent stages
   resolved files but does not commit. Then we verify no conflict markers
   remain (`git diff --check`), commit the merge, and push.
6. **Push is regular**, not force. The merge commit is additive.

On success, emit `task.merge_conflict_resolved` → `on_merge_conflict_resolved`
calls `_attempt_lgtm_merge` again for one retry. On failure, emit
`task.merge_conflict_resolution_failed` → transition to AWAITING_REVIEW.

The Redis flag (TTL 24h, survives process restart, no migration needed) caps
this at one resolution attempt per task: if the second merge call still finds
the PR dirty, the resolver isn't dispatched again.

### Why merge (not rebase)

Rebase + force-push (with-lease or otherwise) was rejected because:

- Force-pushes overwrite the commit history that human reviewers may have
  pinned in their tabs/comments.
- Auto-agent's PRs are squash-merged at the end anyway, so the per-commit
  history on the feature branch is throwaway. A merge commit costs us
  one extra commit on a branch we'll squash, vs rebasing and risking
  collision with concurrent pushes.
- Regular pushes don't need any extra GitHub permissions or settings.

### Why no body-pattern match for LGTM

We considered requiring "LGTM" in the review body. Rejected:

- The natural freeform flow (architect/PO posts review → addresses comments →
  re-reviews) already produces a state-`approved` review when there are no
  outstanding issues. The semantic gate (`approved`) is more reliable than a
  string match (`/LGTM/i`).
- Lets humans use GitHub's native "Approve" button without coaching them on
  body conventions.
- Keeps `state=changes_requested` and `state=commented` paths unchanged.

## Consequences

**Easier:**

- Freeform PRs merge as soon as a review approves them, with no human in the
  loop after that.
- Drift on `main` no longer blocks auto-merge; the resolver pulls base in.
- Non-freeform tasks are entirely unaffected — they stay on the human-review
  path.

**Harder / risks:**

- The CI gate is now `mergeable_state`-based rather than checking individual
  check-runs. If CI is *pending* but `mergeable_state` already says `clean`
  (a brief race window), we may merge before CI completes. Acceptable — the
  reviewer presumably wouldn't approve before CI started, and the second
  retry path (after conflict resolution) re-checks state.
- The conflict-resolver agent has full repo write access during the merge.
  This is necessary because resolving a real conflict often requires
  updating callers, imports, or test fixtures beyond the conflicting file.
  Risk is bounded by: the resolver runs in an isolated workspace, only
  pushes to the feature branch (never `main`), and the resulting merge
  commit goes through the same `_auto_merge_pr` retry — including its own
  conflict re-check.
- Retry budget is tracked via Redis with a 24h TTL. A task whose conflict
  resolution failed can re-trigger after 24h if a new LGTM lands. That's
  by design: the world may have changed (someone fixed `main`).
