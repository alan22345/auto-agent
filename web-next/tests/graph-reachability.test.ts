// ADR-016 Phase 7 §11 — reachability BFS for ancestor / descendant
// highlights.
//
// ``computeAncestors(blob, nodeId)`` walks the edge graph backwards
// (follows incoming edges). ``computeDescendants(blob, nodeId)`` walks
// it forwards. Both:
//
// * always include the starting node in the result,
// * are bounded to a max depth of 20 so pathological deep chains can't
//   wedge the UI,
// * carry a visited Set so cycles (self-loop, A→B→A) terminate.
//
// Cytoscape edge ids are ``${source}->${target}:${kind}`` for the
// canvas class diff, but the reachability helpers operate on node ids
// (Edge.source / Edge.target) directly.

import { describe, it, expect } from 'vitest';
import {
  computeAncestors,
  computeDescendants,
} from '@/lib/graph-reachability';
import type { Edge, RepoGraphBlob } from '@/types/api';

// Mirrors the module-private depth cap in graph-reachability.ts. Kept in
// sync by hand: if the BFS cap changes, the depth-cap test below fails.
const REACHABILITY_MAX_DEPTH = 20;

function evidenceFor(file: string, line: number, snippet: string) {
  return { file, line, snippet };
}

function makeBlob(edges: Edge[]): RepoGraphBlob {
  const ids = new Set<string>();
  for (const e of edges) {
    ids.add(e.source);
    ids.add(e.target);
  }
  return {
    commit_sha: 'sha',
    generated_at: '2026-05-18T00:00:00Z',
    analyser_version: 'phase7-0.7.0',
    areas: [],
    nodes: Array.from(ids).map((id) => ({
      id,
      kind: 'function' as const,
      label: id,
      file: null,
      line_start: null,
      line_end: null,
      area: 'a',
      parent: null,
    })),
    edges,
  };
}

describe('computeAncestors', () => {
  it('returns just the start node when no incoming edges', () => {
    const blob = makeBlob([
      {
        source: 'A',
        target: 'B',
        kind: 'calls',
        evidence: evidenceFor('a.py', 1, ''),
        source_kind: 'ast',
      },
    ]);
    const result = computeAncestors(blob, 'A');
    expect(result).toEqual(new Set(['A']));
  });

  it('walks incoming edges and includes upstream callers + self', () => {
    // C1 → T and C2 → T. Ancestors of T should be {T, C1, C2}.
    const blob = makeBlob([
      {
        source: 'C1',
        target: 'T',
        kind: 'calls',
        evidence: evidenceFor('c1.py', 1, ''),
        source_kind: 'ast',
      },
      {
        source: 'C2',
        target: 'T',
        kind: 'calls',
        evidence: evidenceFor('c2.py', 1, ''),
        source_kind: 'ast',
      },
    ]);
    const result = computeAncestors(blob, 'T');
    expect(result).toEqual(new Set(['T', 'C1', 'C2']));
  });

  it('terminates on a self-loop', () => {
    const blob = makeBlob([
      {
        source: 'A',
        target: 'A',
        kind: 'calls',
        evidence: evidenceFor('a.py', 1, ''),
        source_kind: 'ast',
      },
    ]);
    const result = computeAncestors(blob, 'A');
    expect(result).toEqual(new Set(['A']));
  });

  it('terminates on a cycle A → B → A', () => {
    const blob = makeBlob([
      {
        source: 'A',
        target: 'B',
        kind: 'calls',
        evidence: evidenceFor('a.py', 1, ''),
        source_kind: 'ast',
      },
      {
        source: 'B',
        target: 'A',
        kind: 'calls',
        evidence: evidenceFor('b.py', 1, ''),
        source_kind: 'ast',
      },
    ]);
    const result = computeAncestors(blob, 'A');
    expect(result).toEqual(new Set(['A', 'B']));
  });

  it('stops at the depth cap', () => {
    // Build a chain longer than REACHABILITY_MAX_DEPTH. Final ancestor
    // result should contain the start + exactly the cap-many upstream
    // nodes.
    const chainLen = REACHABILITY_MAX_DEPTH + 5;
    const edges: Edge[] = [];
    for (let i = 0; i < chainLen; i += 1) {
      // Edge ``n{i} → n{i+1}`` — so to reach n0 backwards from
      // n{chainLen}, BFS would need ``chainLen`` hops.
      edges.push({
        source: `n${i}`,
        target: `n${i + 1}`,
        kind: 'calls',
        evidence: evidenceFor('x.py', 1, ''),
        source_kind: 'ast',
      });
    }
    const blob = makeBlob(edges);
    const start = `n${chainLen}`;
    const result = computeAncestors(blob, start);
    // Start node + cap-many ancestors visited.
    expect(result.size).toBe(REACHABILITY_MAX_DEPTH + 1);
    expect(result.has(start)).toBe(true);
  });
});

describe('computeDescendants', () => {
  it('returns just the start node when no outgoing edges', () => {
    const blob = makeBlob([
      {
        source: 'A',
        target: 'B',
        kind: 'calls',
        evidence: evidenceFor('a.py', 1, ''),
        source_kind: 'ast',
      },
    ]);
    const result = computeDescendants(blob, 'B');
    expect(result).toEqual(new Set(['B']));
  });

  it('walks outgoing edges and includes downstream callees + self', () => {
    // S → D1, S → D2, S → D3 — descendants of S should be {S, D1, D2, D3}.
    const blob = makeBlob([
      {
        source: 'S',
        target: 'D1',
        kind: 'calls',
        evidence: evidenceFor('s.py', 1, ''),
        source_kind: 'ast',
      },
      {
        source: 'S',
        target: 'D2',
        kind: 'calls',
        evidence: evidenceFor('s.py', 2, ''),
        source_kind: 'ast',
      },
      {
        source: 'S',
        target: 'D3',
        kind: 'calls',
        evidence: evidenceFor('s.py', 3, ''),
        source_kind: 'ast',
      },
    ]);
    const result = computeDescendants(blob, 'S');
    expect(result).toEqual(new Set(['S', 'D1', 'D2', 'D3']));
  });

  it('terminates on a cycle A → B → A', () => {
    const blob = makeBlob([
      {
        source: 'A',
        target: 'B',
        kind: 'calls',
        evidence: evidenceFor('a.py', 1, ''),
        source_kind: 'ast',
      },
      {
        source: 'B',
        target: 'A',
        kind: 'calls',
        evidence: evidenceFor('b.py', 1, ''),
        source_kind: 'ast',
      },
    ]);
    const result = computeDescendants(blob, 'A');
    expect(result).toEqual(new Set(['A', 'B']));
  });
});
