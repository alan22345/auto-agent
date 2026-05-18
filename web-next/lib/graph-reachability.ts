// ADR-016 Phase 7 §11 — reachability BFS for ancestor / descendant
// highlights.
//
// Pure helpers — operate on the in-memory ``RepoGraphBlob`` without
// touching the network or cytoscape. The page lifts the resulting Set
// into a prop on ``GraphCanvas`` which translates it into two
// classes:
//
// * ``reachability-highlight`` — node is in the subgraph.
// * ``reachability-fade`` — everything else is dimmed.
//
// The start node is always included in the result so the user's
// selected node stays visually prominent.

import type { Edge, RepoGraphBlob } from '@/types/api';

/** Bounded depth so pathological deep chains can't wedge the UI.
 * 20 levels is plenty for a function-level callgraph; deeper chains
 * almost always indicate a layering breach the user can investigate
 * via the violations panel. */
export const REACHABILITY_MAX_DEPTH = 20;

/**
 * Compute the set of ancestor node ids — nodes that have a path TO
 * ``startId`` by following incoming edges (i.e. callers, parents).
 *
 * Always includes ``startId``. Bounded to ``REACHABILITY_MAX_DEPTH``;
 * cycles terminate via the visited set.
 */
export function computeAncestors(
  blob: RepoGraphBlob,
  startId: string,
): Set<string> {
  return bfs(blob, startId, 'incoming');
}

/**
 * Compute the set of descendant node ids — nodes ``startId`` can
 * reach by following outgoing edges (i.e. callees, children).
 *
 * Always includes ``startId``. Bounded to ``REACHABILITY_MAX_DEPTH``;
 * cycles terminate via the visited set.
 */
export function computeDescendants(
  blob: RepoGraphBlob,
  startId: string,
): Set<string> {
  return bfs(blob, startId, 'outgoing');
}

// ---------------------------------------------------------------------------
// Internals
// ---------------------------------------------------------------------------

type Direction = 'incoming' | 'outgoing';

function bfs(
  blob: RepoGraphBlob,
  startId: string,
  direction: Direction,
): Set<string> {
  // Pre-build the adjacency list once so each call is O(V + E) rather
  // than O(V * E). For the bounded UI use this is invisible; for
  // larger graphs the alternative would re-scan ``blob.edges`` at
  // every hop.
  const adj = buildAdjacency(blob.edges as Edge[], direction);

  const visited = new Set<string>([startId]);
  // Each queue entry carries the depth so we can hard-stop at the cap
  // without an outer counter.
  let frontier: string[] = [startId];
  for (let depth = 0; depth < REACHABILITY_MAX_DEPTH; depth += 1) {
    if (frontier.length === 0) break;
    const next: string[] = [];
    for (const id of frontier) {
      const neighbours = adj.get(id);
      if (!neighbours) continue;
      for (const n of neighbours) {
        if (visited.has(n)) continue;
        visited.add(n);
        next.push(n);
      }
    }
    frontier = next;
  }
  return visited;
}

function buildAdjacency(
  edges: Edge[],
  direction: Direction,
): Map<string, string[]> {
  const m = new Map<string, string[]>();
  for (const e of edges) {
    // For ancestors (incoming): from the target's perspective the
    // neighbour we hop to is the source. For descendants (outgoing):
    // from the source's perspective the neighbour is the target.
    const from = direction === 'incoming' ? e.target : e.source;
    const to = direction === 'incoming' ? e.source : e.target;
    const list = m.get(from) ?? [];
    list.push(to);
    m.set(from, list);
  }
  return m;
}
