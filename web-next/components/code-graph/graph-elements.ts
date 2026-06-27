// Cytoscape element builder (ADR-016 — code-graph canvas).
//
// Pure transform from a ``RepoGraphBlob`` into the element array handed
// to cytoscape, plus the edge-colour palette it keys off. Kept in its
// own module so the (non-trivial, regression-tested) mapping logic can
// be unit-tested directly without standing up a real cytoscape canvas.

import type { Edge, Node, RepoGraphBlob } from '@/types/api';

export const EDGE_COLOUR: Record<string, string> = {
  calls: '#3b82f6',
  imports: '#9ca3af',
  inherits: '#a855f7',
  http: '#f97316',
};

// Destructive overlay applied to boundary-violation edges. Kept as a
// constant so the unit test can assert it and the violations panel can
// re-use the same hue for selected rows in a future polish pass.
export const VIOLATION_COLOUR = '#ef4444';

interface CyElement {
  data: Record<string, unknown>;
  classes?: string;
}

export interface BuildElementsOptions {
  /** Edge id (``source->target:kind``) to mark with ``highlighted=1`` so
   * the cytoscape selector can lift it visually. ``null`` / omitted =
   * no highlight. */
  highlightedEdgeId?: string | null;
}

export function blobToCytoscapeElements(
  blob: RepoGraphBlob,
  areaErrorById: Record<string, string | null>,
  options: BuildElementsOptions = {},
): CyElement[] {
  const elements: CyElement[] = [];
  const highlightedEdgeId = options.highlightedEdgeId ?? null;
  const nodeIds = new Set<string>();

  for (const n of blob.nodes as Node[]) {
    nodeIds.add(n.id);
    const failed = areaErrorById[n.id] != null;
    elements.push({
      data: {
        id: n.id,
        label: n.label,
        kind: n.kind,
        area: n.area,
        parent: n.parent ?? undefined,
        failed: failed ? 1 : undefined,
        error: failed ? areaErrorById[n.id] : undefined,
      },
    });
  }

  for (const e of blob.edges as Edge[]) {
    // Defensive filter: cytoscape silently drops edges whose endpoints
    // aren't in the node set AND its compound-graph layout
    // (cose + expand-collapse) breaks when phantom edges are present,
    // leaving every node positioned at the origin. The pipeline tries
    // to keep these out; this is a belt-and-braces guard so a single
    // unresolved ``module:`` placeholder can never render the canvas
    // blank.
    if (!nodeIds.has(e.source) || !nodeIds.has(e.target)) continue;
    const id = `${e.source}->${e.target}:${e.kind}`;
    const isViolation = e.boundary_violation === true;
    const isLlmDeduced = e.source_kind === 'llm';
    elements.push({
      data: {
        id,
        source: e.source,
        target: e.target,
        kind: e.kind,
        color: isViolation
          ? VIOLATION_COLOUR
          : (EDGE_COLOUR[e.kind] ?? '#9ca3af'),
        snippet: e.evidence.snippet,
        evidenceFile: e.evidence.file,
        evidenceLine: e.evidence.line,
        sourceKind: e.source_kind,
        // Phase 7 §11 — boolean class fed to the
        // ``edge[?sourceKindLlm]`` selector so LLM-deduced edges render
        // dotted + slimmer. Left undefined (not ``false``) for AST
        // edges so the selector simply doesn't match — same convention
        // as ``boundaryViolation`` and ``highlighted``.
        sourceKindLlm: isLlmDeduced ? true : undefined,
        boundaryViolation: isViolation ? 1 : undefined,
        violationReason: e.violation_reason ?? undefined,
        highlighted: highlightedEdgeId === id ? 1 : undefined,
      },
    });
  }

  return elements;
}
