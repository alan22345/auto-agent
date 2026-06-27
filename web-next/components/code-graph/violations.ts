// Boundary-violation derivation (ADR-016 §7 — Phase 5).
//
// Pure helpers that turn a ``RepoGraphBlob`` into the rows rendered by
// ``ViolationsPanel``. Kept in their own module so the derivation logic
// can be unit-tested directly without standing up the React component.

import type { Edge, Node, RepoGraphBlob } from '@/types/api';

interface ViolationRow {
  edgeId: string;
  sourceLabel: string;
  targetLabel: string;
  reason: string;
}

function violationEdgeId(edge: Edge): string {
  // Same canonical form the graph-canvas uses so the parent page can
  // correlate panel rows with cytoscape edge data.
  return `${edge.source}->${edge.target}:${edge.kind}`;
}

export function findViolations(blob: RepoGraphBlob): ViolationRow[] {
  const labels = new Map<string, string>();
  for (const n of blob.nodes as Node[]) {
    labels.set(n.id, n.label ?? n.id);
  }
  const out: ViolationRow[] = [];
  for (const e of blob.edges as Edge[]) {
    if (!e.boundary_violation) continue;
    out.push({
      edgeId: violationEdgeId(e),
      sourceLabel: labels.get(e.source) ?? e.source,
      targetLabel: labels.get(e.target) ?? e.target,
      reason: e.violation_reason ?? 'boundary_violation',
    });
  }
  return out;
}
