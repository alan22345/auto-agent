# Repo-Graph — Capability / Flow Map Design Spec

## Summary

Replace the default `/code-graph/{repo}` view with a four-LOD **capability-and-flow map**: at the top level the repo decomposes into ~5-12 named *capabilities* (things the system does), each capability into a handful of *flows* (request-paths from one entry point to one terminal effect), each flow into an ordered chain of *steps* (functions), and finally the source of a step. The current cytoscape compound graph remains as a fallback "Raw graph" tab.

The map is a post-processing view over the existing `RepoGraph.graph_json` plus a new derivation pass that traces flows from detected entry points and LLM-labels the result. No changes are required to the analyzer pipeline in `agent/graph_analyzer/`.

## Context

`ADR-016` shipped a hierarchical, function-level, cited-edge graph that renders via cytoscape with expand-collapse compound nodes. The May 21 readability layers (fcose, edge bundling, area filter, expand/collapse toolbar) improved the small-repo case but Cardamon's 3031-node / 6056-edge graph remains an unreadable smear when areas are expanded. The user reported that the current visualisation does not deliver intuitive understanding of how a repo works.

The root problem isn't rendering polish — it's the choice of primitive. A human grasping a new repo doesn't think "`lib/cache.py` is connected to `routes/auth.py`"; they think "when a user logs in, the request hits `/auth/login`, middleware validates it, an OAuth provider is called, a session row is written, a cookie is set." The unit of understanding is **a behaviour the system performs**, not a node in the import graph. Clustering implementation detail produces a clearer map of the same wrong thing.

An earlier draft of this spec routed through algorithmic clustering (Louvain at two resolutions over the file/function graph with TF-IDF edge weighting and hub disambiguation). That direction was discarded because it still treats files and functions as the primary objects the user navigates. Files are *implementation detail of capabilities*. Capabilities are what a reader actually wants to see first.

The auto-LOD-on-scroll prototype from the same May 21 commit is treated here as established prior art: zoom must be a discrete user action, never a side effect of pan/zoom. The earlier failure pattern is referenced explicitly in the LOD-transition section.

## Decision

### 1. Primary view is the capability / flow map; raw graph is a fallback tab

`/code-graph/{repo}` opens on the new map by default. A tab bar exposes **Map** (default) and **Raw graph** (the existing cytoscape compound view, unchanged). The map is purely derived from `graph_json` plus a new `flow_json` derivation; no re-analysis of the underlying repo is needed when the map view is added.

Repos that have no detectable entry points (pure libraries, framework projects) render an empty-state on the Map tab pointing the user to Raw graph. This is a v1-honest fallback rather than an attempt to fabricate a map.

### 2. Four LODs — capability, flow, step, source

| LOD | Tile content | Typical count | Edges visible |
|-----|--------------|---------------|---------------|
| 0 — Capability | Named tile + one-sentence description + ordered list of top flows | 5-12 capabilities | Bundled inter-capability dependencies, kind-coloured |
| 1 — Flow | Named tile per flow within the focused capability + trigger + outcome | 3-10 flows per capability | Internal flow→flow edges within the capability; boundary ports to other capabilities |
| 2 — Step | Ordered chain of functions on the focused flow with branch/error edges | 5-15 steps per flow | Internal call edges; boundary ports labelled with sibling flows that also use a step |
| 3 — Source | Code preview of the focused step | One function/method body | None (panel view) |

A single LOD is rendered at a time. Drilling in/out is always a discrete action — never triggered by scroll, never triggered by layout reflow. See §6 below.

### 3. Flow extraction

A new module `agent/graph_analyzer/flows.py` runs after `pipeline.assemble_graph`. Stages:

1. **Entry-point detection.** Nodes that satisfy any of:
   - Have an incoming `http` edge (already in `graph_json` — TypeScript fetch/axios → route handler edges from cross-language matching).
   - Carry a route-handler decorator on the AST (FastAPI `@router.get`, Click `@command`, etc.). Heuristic set lives in `flows.py`, extended per-language.
   - Are referenced from a queue-consumer registration (Redis BLPOP wrapper, Celery `@app.task`, etc. — small per-stack heuristic table).
   - Are referenced from a scheduled-job registration (cron-style decorators, APScheduler entries).
   - Are CLI entry points (`if __name__ == "__main__"`, `click.group()`, `argparse.ArgumentParser` invocations bound to module-level `main`).

   Each match is tagged with its kind (`http`, `queue`, `cron`, `cli`) so flows can be filtered.

2. **Forward trace.** From each entry point, walk the call-graph forward. Terminate a flow at any of: response return (HTTP), queue publish, external HTTP call, database write (ORM/session call). Each terminus is tagged so the UI can render the outcome icon.

3. **Branch handling.** A node with multiple outgoing call edges produces branches. Branches up to depth 3 are inlined into the flow; deeper branches are summarised as `+N sub-paths` collapsed nodes the user can expand at LOD 2.

4. **Cycles.** A cycle that returns to a node already on the path is rendered as a back-edge with a loop indicator, not expanded again. Mutual recursion between two functions resolves to a single labelled edge.

5. **Capability grouping.** Flows are grouped into capabilities by a single structured LLM call: input is the list of flows with their entry-point names, terminal outcomes, and the names of their top-three steps; output is `{capability_name, capability_description, flow_ids}` records. The model is instructed to produce 5-12 capabilities total and to leave a flow ungrouped (Capability: "Other") only if it does not fit naturally.

6. **Unreached tray.** Functions and files that are not reached by any flow's forward trace from any detected entry point are collected into an `unreached` list in `flow_json`. They are rendered at the bottom of LOD 0 as a single compact tile labelled "Unreached (N nodes)". Clicking the tile reveals the list with a "Open in Raw graph" link per item. Tests, type definitions, and dead code typically end up here. The tray is deliberately small and unembellished — its purpose is to make the existence of un-traced code legible without competing with the named capabilities for attention.

### 4. LLM labelling with file-hash invalidation

Both capability names+descriptions and flow names+descriptions are LLM-generated and persisted. Re-generation is **content-keyed**, not time-keyed.

Persisted alongside each flow:

```jsonc
{
  "flow_id": "auth_google_login_a1b2",
  "name": "Google OAuth Login",
  "description": "User clicks 'Sign in with Google'…",
  "entry_point": "api/auth/google.py::login_redirect",
  "terminal": { "kind": "response", "node": "api/auth/google.py::callback" },
  "step_node_ids": [...],
  "file_set": ["api/auth/google.py", "api/auth/sessions.py", "lib/cookies.py"],
  "file_set_hash": "sha256:abc123…",
  "labeled_at_commit": "sha:7e9f…"
}
```

On each refresh:
- Compute the current `file_set` for the flow (the union of files containing any step on the flow).
- Compute a `sha256` of the concatenated file contents (sorted by path) for that set.
- If the hash matches the persisted `file_set_hash`, reuse the stored name+description. No LLM call.
- If the hash differs, regenerate name+description with the LLM and update the persisted record.
- If the flow no longer exists (entry point removed), drop the record.
- If a new flow appears, label it from scratch.

Capabilities apply the same pattern with a different key: the hash is over the sorted list of `flow_id`s that compose them. If the membership is unchanged across refreshes, the name and description persist; if it changes, regenerate. This means a capability survives a flow being added or removed only if the user accepts a rename (the LLM may produce the same name; it may not).

This caching contract is the user's explicit requirement and is load-bearing: it bounds LLM cost on refresh to "only changed surfaces" and it makes the user's mental map of the repo durable across refreshes.

### 5. Boundary ports at flow boundaries

When focused at LOD 2 on one flow, calls into steps that are also part of *other* flows in the same capability render as ports on the right edge of the flow tile, labelled `→ also used in: <flow_name>, <flow_name>`. Clicking the port pans/drills to the sibling flow.

When focused at LOD 1 inside one capability, edges to steps that live in *another* capability render as ports labelled `→ <capability_name>`. Clicking drills out one LOD and pans to the destination.

This is the same boundary-port pattern proposed for the earlier cluster-based design, just applied to capability/flow boundaries instead of region boundaries.

### 6. LOD transitions are discrete actions

- **Scroll / pinch** changes CSS scale of the current LOD only. Range 0.5×-2×. Never triggers LOD change. This rules out the failure mode of the May 21 auto-LOD prototype where layout reflow on collapse changed effective zoom and oscillated the LOD selection.
- **Double-click a tile** drills in.
- **`Esc`, breadcrumb segment click, `−` button, or `↑` arrow** drills out one LOD.
- **`+` button or `↓` arrow** drills into the currently selected tile (keyboard parity with mouse).
- **`Home`** returns to LOD 0.

URL state encodes the current focus path: `/code-graph/{repo}/map?p=<capability_id>/<flow_id>/<step_id>`. Deep links and browser back/forward work. The Raw tab uses a separate URL fragment so switching tabs preserves both views' independent state.

### 7. Rendering

A new component `web-next/components/code-graph/map-canvas.tsx` is added beside the existing `graph-canvas.tsx`. The map canvas does **not** use `cytoscape-expand-collapse` — every LOD is rendered as a freshly-built cytoscape instance with that LOD's small element set (~30-50 nodes typical) using fcose. LOD swaps cross-fade between two stacked cytoscape canvases via CSS opacity over ~300ms; both canvases are remounted on swap so layout determinism is preserved.

The page `web-next/app/(app)/code-graph/[repoId]/page.tsx` gains a tab bar: **Map** (default) and **Raw graph**. Tab state is part of the URL. The existing search, edge-kind filter, and area filter components are reused; the area filter is hidden in the Map view (irrelevant) and the search behaviour is scoped to the current LOD ("find a capability", "find a flow", "find a step").

Boundary-port DOM elements are rendered as siblings of the cytoscape host, positioned over tile-edge coordinates computed from the cytoscape instance. This is the same DOM-sibling overlay pattern adopted in commits `0a9c75b` and `c42c99a` (which fixed the click-through bug for canvas overlays).

### 8. Agent integration

The existing `query_repo_graph` tool is untouched — it queries `graph_json` directly. The capability/flow derivation is a viewer concern by default.

A small additive op is added: `which_capability(node)` → `{capability_id, capability_name, capability_description, flow_ids, flow_names}` for a given function or file. Useful when the agent wants to reason in product-language ("this change touches the Authentication capability") instead of structural language ("this change touches `agent/auth/`"). The op reads from the persisted `flow_json`.

The system-prompt nudge in `agent/context/system.py` adds one paragraph when a repo has flows derived: *"You can call `query_repo_graph(repo_id, 'which_capability', {node})` to see which user-visible capability a function belongs to."*

### 9. Storage

One new column on `RepoGraph`:

```
flow_json: JSONB nullable
  {
    "capabilities": [
      {
        "id": "auth_d3a8",
        "name": "Authentication",
        "description": "…",
        "flow_ids": [...],
        "flow_membership_hash": "sha256:…",
        "labeled_at_commit": "sha:…"
      }, …
    ],
    "flows": [
      { flow record as in §4 }, …
    ],
    "computed_at_commit": "sha:…",
    "labeler_model": "claude-haiku-4-5"
  }
```

Migration `037_repo_graph_flows.py` adds the column nullable. When `flow_json` is null, the Map tab shows an empty state with a **"Compute capability map"** button that runs the derivation+labelling job (~5-30s typically for medium repos, longer for cardamon-scale). The button reappears as **"Recompute map"** in the freshness banner once a map exists. Recompute reuses the file-hash cache from §4 so the typical recompute is cheap.

### 10. Test coverage

- `flows.py` unit tests: entry-point detection per kind (`http`, `queue`, `cron`, `cli`) on fixture repos in `tests/fixtures/graph_repo_*`.
- Forward-trace correctness: fixture flows with known step chains; assert traces match including branch and cycle handling.
- File-hash invalidation: build a flow, hash its files, modify one file's content, re-derive — assert only that flow's name+description are re-requested from the (mocked) LLM. Modify a file *not* on the flow, assert no LLM call.
- Capability membership hashing: add/remove a flow from a capability, assert the capability is re-labelled; leave membership unchanged, assert no re-label.
- Boundary-port projection: for a fixture LOD 2 view, assert ports cover exactly the cross-flow edges, no more, no less.
- `which_capability` agent op: fixture flows → assert the right capability returned, including the "Other" fallback for ungrouped flows.
- `map-canvas` vitest: LOD swap preserves selection, breadcrumb reflects current path, URL updates, browser back/forward navigates LODs.

### 11. Phasing

Each phase ships in a deployable state with no stubs.

1. **`flows.py` derivation + migration + Compute endpoint + agent op `which_capability`.** No UI yet; verified via the agent op and direct DB inspection.
2. **LLM labelling pass with file-hash invalidation.** Capabilities + flows get names and descriptions stored; recompute reuses cache.
3. **`map-canvas.tsx` LOD 0 and LOD 1 + tab bar on the repo page.** User can land on Map, see capabilities, drill to flows.
4. **LOD 2 (step chains) and LOD 3 (source).** Boundary ports at flow boundaries.
5. **URL state, deep links, keyboard navigation, polish.** Cross-fade transitions, breadcrumb, empty states.

## Consequences

### What becomes easier

- A reader landing on an unfamiliar repo sees the repo's *behaviours* in seconds, not its directory structure.
- The agent can phrase reasoning in product-language ("modifies the Authentication capability") via `which_capability`, which is what PR descriptions and Slack messages need.
- File reorganisations stop disrupting the visual model: capabilities and flows persist across refactors as long as the entry-point and terminal stay recognisable.
- Refresh cost is bounded by what actually changed (file-hash gate), so manual recompute is fast in the common case.

### What becomes harder

- The map view depends on the LLM being available at first compute and on labels surviving as JSON in the database. If the LLM is unreachable on first compute, the user sees a clear error and can fall back to the Raw tab.
- Repos with no detectable entry points have no map. This is honest — there is no behaviour to trace — but it means we ship two clearly-different "what you see" outcomes (map vs raw-only) and the empty state on the map tab has to make the situation legible.
- Entry-point detection is per-stack heuristic. We start with HTTP routes (already present in the graph from cross-language matching), FastAPI/Click decorators (Python), Celery/Redis-queue patterns (Python), Next.js routes (TypeScript). Adding a new stack requires adding to the heuristic table; missing a stack means missing flows in repos that use it.

### Risks named honestly

1. **LLM mis-grouping of flows into capabilities.** A capability boundary that doesn't match the user's mental model surfaces a wrong story. Mitigation: `.auto-agent/graph.yml` gains an optional `capabilities` section letting the user pin flow IDs into named capabilities; the LLM only groups un-pinned flows.
2. **Entry-point detection misses on bespoke patterns** (custom framework, RPC over websocket, in-process pub/sub). Mitigation: per-stack heuristic table, easy to extend; un-traced code falls into the Unreached tray (§3 step 6) where its existence remains visible even without a flow.
3. **Flow trace explosion on highly-branching code** (visitor patterns, large switch statements). Mitigation: depth-3 branch inlining + summarised deeper branches; flows always cap at a configurable max-step count (default 50) with the tail collapsed.
4. **Map drifts silently while the graph is fresh.** This is avoided by the per-flow file-hash invalidation contract: any modified file on a flow forces a re-label of that flow on the next recompute. There is no time-based staleness; the user explicitly chooses when to recompute.
5. **Cross-fade LOD swap costs at large per-LOD element counts.** Mitigation: hard cap of 80 elements per LOD via collapsing deeper branches/sub-flows; cytoscape instance is destroyed after the fade-out finishes to avoid leaking.

## Out of scope for v1

- Auto-recompute on push or on schedule. Manual recompute only, matching the existing graph-refresh discipline.
- 3D / WebGL rendering. Not needed at these element counts.
- Mobile support. `/code-graph` is desktop-only today.
- Cross-LOD search (search at LOD 0 jumping straight to a matching step at LOD 3).
- Per-capability ACLs.
- Algorithmic (Louvain-style) clustering as a secondary view. If the capability/flow map proves insufficient for some users, this can be revisited additively.
- Pure-library repos with no entry points get no map. They use the Raw tab; an "Add custom entry point" affordance is deferred.

## File-by-file impact summary

- **Add** `agent/graph_analyzer/flows.py` — entry-point detection, forward trace, branch/cycle handling.
- **Add** `agent/graph_analyzer/flow_labeler.py` — LLM call + file-hash cache.
- **Add** `migrations/versions/037_repo_graph_flows.py` — adds `RepoGraph.flow_json` column.
- **Edit** `shared/models.py` — column declaration.
- **Edit** `shared/types.py` — Pydantic models for capability, flow, step.
- **Edit** `agent/tools/repo_graph.py` (or the file housing `query_repo_graph`) — add `which_capability` op.
- **Edit** `agent/context/system.py` — system-prompt nudge mentioning `which_capability`.
- **Edit** `orchestrator/router.py` — `POST /repos/{repo_id}/graph/flows/recompute` endpoint.
- **Add** `web-next/components/code-graph/map-canvas.tsx` — new view component.
- **Add** `web-next/components/code-graph/capability-tile.tsx`, `flow-tile.tsx`, `step-chain.tsx`, `boundary-port.tsx`.
- **Edit** `web-next/app/(app)/code-graph/[repoId]/page.tsx` — add tab bar, default to Map.
- **Edit** `web-next/lib/code-graph.ts` — fetchers for `flow_json` and recompute endpoint.
- **Tests:** vitest for components; pytest for derivation, labeller, invalidation, agent op.
