# Code-graph Health tab — design

**Date:** 2026-06-08
**Status:** Approved (design phase)
**Area:** `web-next/` (code-graph UI)

## Problem

The code-graph analysis (ADR-016 + the #63 quality layer) computes six quality
dimensions — import/call **cycles**, **dead code**, **clones**, churn
**hotspots**, per-file **maintainability/health**, and a repo-level **health
score** — plus per-node complexity (`cyclomatic`, `cognitive`, `loc`). All of it
is persisted in `RepoGraph.graph_json` and **already returned to the browser** by
`GET /api/repos/{repo_id}/graph/latest` (the full `RepoGraphBlob`). But the UI
only renders nodes, edges, and boundary violations, so today the code-graph page
*looks* like there is almost nothing to report (e.g. "only 1 boundary
violation"), when in fact the findings exist and are simply not displayed.

This is a **rendering gap in `web-next`**, not a backend gap.

## Goal

Surface all six quality dimensions plus per-node complexity on the
`code-graph/[repoId]` page so the analysis output is actually visible.

## Acceptance criteria

- A third **Health** tab appears beside **Map** and **Raw** on the code-graph
  page.
- The Health tab shows a repo-health scorecard (0–100 score + counts for cycles,
  clones, dead code, hotspots, poor-maintainability files) and a collapsible
  section per dimension listing the findings.
- Selecting a `function` node on the Raw tab shows its `cyclomatic`,
  `cognitive`, and `loc` complexity in the node side-panel.
- A section with no findings renders an explicit "No findings" state (clean ≠
  hidden).
- A blob that predates the quality layer (`blob.health == null`) renders a
  "re-run refresh to compute health" banner — distinguishing *not computed* from
  *zero findings*.
- `web-next/types/api.ts` is regenerated so the quality fields are typed.
- `tsc --noEmit`, `next lint`, and the vitest suite pass.

## Non-goals (v1)

- Click-a-finding-to-highlight-the-node cross-linking from Health → Raw graph
  (needs shared cross-tab selection state). Logged as a follow-up.
- Tinting graph nodes by health band.
- Any backend/API/Pydantic change.

## Architecture & data flow

No backend changes. `RepoGraphBlob` (shared/types.py) already carries `cycles`,
`dead_code`, `clones`, `hotspots`, `file_health`, `health`, and per-node
`cyclomatic`/`cognitive`/`loc`. The endpoint passes the blob through unfiltered
(`orchestrator/router.py` `get_latest_repo_graph`).

1. **Types regen** — run `python3 scripts/gen_ts_types.py` so
   `web-next/types/api.ts` gains `RepoHealth`, `DependencyCycle`,
   `DeadCodeFinding`, `CloneGroup`, `CloneInstance`, `Hotspot`, `FileHealth`,
   and the new `RepoGraphBlob` / `Node` fields.
2. **Page wiring** — `web-next/app/(app)/code-graph/[repoId]/page.tsx` gains a
   third `<TabsTrigger value="health">` + `<TabsContent value="health">`
   rendering `<HealthTab blob={latest.blob} />`. The data is already in hand via
   the existing `useRepoGraph` hook (`latest.blob`); no new fetch/hook.

## Components

All new components live in `web-next/components/code-graph/`, each with one
concern (~80–150 lines), built from existing shadcn primitives (`card`,
`badge`, `button`, `scroll-area`, `separator`) + Tailwind. No new deps:
collapsibles use a small local `useState` toggle; tables are Tailwind-styled
`<table>` elements mirroring `violations-panel.tsx`.

| Component | Renders | Data |
|-----------|---------|------|
| `health-tab.tsx` | Container: scorecard + six sections; empty/stale handling | `blob: RepoGraphBlob` |
| `health-scorecard.tsx` | Score (0–100) as a labelled bar + count cards | `health: RepoHealth` |
| `collapsible-section.tsx` | Shared header (title + count + chevron) toggle wrapper | `title`, `count`, `children` |
| `cycles-section.tsx` | `DependencyCycle` list: member chain + import/call kind | `cycles: DependencyCycle[]` |
| `dead-code-section.tsx` | Table: kind, target, file, reason | `deadCode: DeadCodeFinding[]` |
| `clones-section.tsx` | Clone families: mode, token_len, instance `file:line` list | `clones: CloneGroup[]` |
| `hotspots-section.tsx` | Table: file, churn, complexity_density, score, trend | `hotspots: Hotspot[]` |
| `file-health-section.tsx` | Table: file, maintainability index, band badge, crap | `fileHealth: FileHealth[]` |

Plus an edit to the existing `node-side-panel.tsx`: when the selected node is
`kind === "function"`, render `cyclomatic` / `cognitive` / `loc` as badges.

## Error / edge-case handling

- `blob.health === null` → stale banner; sections still render their arrays
  (which will also be empty on old blobs).
- Empty dimension array → "No findings" row inside the (still-visible) section.
- Long file lists → wrap each section body in the existing `scroll-area` with a
  max height so the tab does not grow unbounded.

## Testing

TDD with vitest + `@testing-library/react`:

- One test file per section component: renders findings from a fixture, plus the
  empty state.
- `health-tab.test.tsx`: renders scorecard + sections from a full fixture;
  asserts the stale banner appears when `health == null`.
- `node-side-panel` test: complexity badges appear for a `function` node, absent
  otherwise.
- Fixture derived from the real repo-170 blob shape.
- Gate: `npm run test`, `tsc --noEmit`, `next lint`, then a manual render of the
  Health tab against the live VM API.

## Files

**Changed:** `web-next/types/api.ts` (regen),
`web-next/app/(app)/code-graph/[repoId]/page.tsx`,
`web-next/components/code-graph/node-side-panel.tsx`.

**New:** `health-tab.tsx`, `health-scorecard.tsx`, `collapsible-section.tsx`,
`cycles-section.tsx`, `dead-code-section.tsx`, `clones-section.tsx`,
`hotspots-section.tsx`, `file-health-section.tsx` (+ co-located `*.test.tsx`),
all under `web-next/components/code-graph/`.
