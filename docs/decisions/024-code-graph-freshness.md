# [ADR-024] Code graph freshness — honest drift + refresh-on-change

> **Summary:** Measure graph drift against origin (`git ls-remote`, TTL-cached) instead of the frozen analyser workspace, and self-heal staleness with a debounced refresh whenever the analysis branch moves (push webhook, PR-merged webhook, auto-merge hook) — lifting ADR-016's v1 "manual refresh only" deferral.

## Status

Accepted

Supplements [ADR-016] and [ADR-023]. Lifts the "auto-refresh on push" deferral both of those ADRs recorded.

## Context

ADR-016 shipped a `staleness` envelope so agents could decide whether to trust a stored graph, and deferred auto-refresh ("manual refresh only in v1 — avoids the 'graph thinks it's fresh but isn't' failure mode"). In practice the v1 mechanism produced exactly that failure mode, inverted: `compute_staleness` compared the graph's SHA against the **analyser workspace's** HEAD — which only moves during a refresh. Once an analysis landed, the two SHAs matched forever; origin could accumulate fifty commits and every query still reported `drifted: false`. The flag effectively measured "did our own refresh finish," not "is the graph behind reality."

ADR-023 raised the stakes: `get_symbol_source` serves actual code bytes from that frozen workspace, so a false-fresh signal now feeds the agent outdated source, not just outdated topology. And with no automatic refresh, the only thing keeping the graph current was a human remembering to click Refresh.

## Decision

Two halves: tell the truth about staleness, and shrink the window in which staleness exists.

### 1. Drift is measured against origin

`compute_staleness` gains an `analysis_branch` parameter. When supplied, it asks origin directly — `git ls-remote origin refs/heads/<branch>` from the analyser workspace (no fetch, one network round-trip) — and `drifted = graph_sha != origin_sha`. Results, including failures, are TTL-cached for 60s (`ORIGIN_CACHE_TTL_SECONDS`): graph queries arrive in bursts within a task, and a stale-by-a-minute origin SHA is still categorically more honest than never asking.

Fallback ladder when origin can't be asked: workspace-HEAD comparison (the legacy semantics), then `drifted: true` when nothing is readable. The envelope and `GET /graph/staleness` gain `origin_sha` so both the agent and the freshness banner can show *how far* reality has moved. The system-prompt nudge now describes `drifted` accurately and tells the agent what to do about it (prefer reading the file directly).

### 2. Refresh-on-change, debounced

`orchestrator/graph_freshness.py::request_graph_refresh_soon(repo_id, branch=…)` is the single trigger seam. After a quiet period (30s trailing-edge debounce per repo) it publishes the existing `repo.graph_requested` event — the established refresh handler does the rest, per-repo flock included. Contracts the triggers rely on: best-effort and never raises (a refresh must never break a merge path), bursts collapse to one event, non-analysed branches are filtered out, and repos without a completed analysis are ignored (first analysis stays an explicit onboarding action).

Three call sites, intentionally overlapping (the debounce dedupes):

- **`push` webhook** (new handler) — fires for any branch move; covers human pushes. Requires the GitHub App to subscribe to push events.
- **`pull_request` closed+merged webhook** — fires for *any* merged PR, including human PRs whose head branch carries no `auto-agent/` prefix; uses the PR's base branch for the filter.
- **`_auto_merge_pr` success** in `run.py` — belt-and-braces for deployments without webhooks configured; auto-agent's own merges are the dominant source of change in an autonomously-developed repo.

## Consequences

### What becomes easier

- The agent can actually trust `drifted: false` — it now means "origin agrees," not "we haven't looked."
- Staleness self-heals: within roughly a debounce window plus one analysis run of any merge, the graph is current again, with zero human action.
- The freshness banner can distinguish "workspace moved" from "origin moved" via `origin_sha`.

### What becomes harder / risks

- One `ls-remote` per repo per minute of active querying — a network dependency inside the query path. Mitigated by the TTL cache, the cached-failure rule, and the fallback ladder; a dead remote degrades to the old behaviour, never an error.
- Refresh cost now scales with merge frequency. Bounded by the debounce, the LLM gap-fill caps that already exist, and the per-repo flock. A refresh that loses the flock race is simply skipped — drift persists until the next trigger, which is acceptable because triggers recur.
- The push path silently does nothing until the GitHub App subscribes to `push` events — an ops checklist item, mitigated by the other two triggers.

### Deferred

- **Scheduled/cron refresh** as a backstop for repos that change without webhooks or auto-merges.
- **UI surfacing of `origin_sha`** in the freshness banner (the field is in the payload; the banner still keys off `drifted`).
- **Skipping analysis when origin's tip already matches the stored graph** at refresh time (cheap optimisation inside the refresh handler).
