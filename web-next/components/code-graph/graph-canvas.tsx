'use client';
// Cytoscape compound graph renderer (ADR-016 §11, Phase 2).
//
// Nodes nest: area → file → class → function. All areas start
// collapsed; the cytoscape-expand-collapse extension provides the
// toggle UI when the user clicks. Edge colours are keyed off ``kind``
// (calls=blue, imports=grey, inherits=purple) — Phase 7 polishes this;
// here we ship a usable default.
//
// Phase 5 (ADR-016 §7) overlays a destructive style on edges with
// ``boundary_violation === true``: thicker red dashed stroke that
// overrides the kind-based colour. The flag + reason are carried on
// the element data so a future side-panel can surface them.
//
// Failed areas (``AreaStatus.status === 'failed'``) get a red border
// and surface their error through the node's tooltip data so users see
// *why* an area's interior is missing.

import { useEffect, useMemo, useRef, useState } from 'react';
import type cytoscape from 'cytoscape';
import type { RepoGraphBlob, AreaStatus, Edge, Node } from '@/types/api';
import { AreaRefreshOverlay } from './area-refresh-overlay';

const EDGE_COLOUR: Record<string, string> = {
  calls: '#3b82f6',
  imports: '#9ca3af',
  inherits: '#a855f7',
  http: '#f97316',
};

// Destructive overlay applied to boundary-violation edges. Kept as a
// constant so the unit test can assert it and the violations panel can
// re-use the same hue for selected rows in a future polish pass.
const VIOLATION_COLOUR = '#ef4444';

interface Props {
  blob: RepoGraphBlob;
  className?: string;
  highlightedEdgeId?: string | null;
  /** Optional — when provided, enables the per-area refresh overlay
   * (ADR-016 Phase 7 §10). Pages without a repo context (e.g.
   * standalone fixture renders in tests) can omit this. */
  repoId?: number;
  /** Phase 7 — node click selects a node for the side panel. */
  onNodeClick?: (nodeId: string) => void;
  /** Phase 7 — edge click opens the evidence popover. The position is
   * the rendered pixel position inside the canvas container, used to
   * anchor a portal. */
  onEdgeClick?: (edgeId: string, pos: { x: number; y: number }) => void;
  /** Phase 7 P2 — search query (case-insensitive substring on node
   * label). Empty / whitespace = no filter applied. */
  searchQuery?: string;
  /** Phase 7 P2 — edge kinds to hide. Defaults to no filter. Each
   * unchecked kind becomes a per-kind class with ``display: none`` so
   * the user can flip kinds on/off without rebuilding elements. */
  hiddenEdgeKinds?: Set<Edge['kind']>;
}

export function GraphCanvas({
  blob,
  className,
  highlightedEdgeId,
  repoId,
  onNodeClick,
  onEdgeClick,
  searchQuery,
  hiddenEdgeKinds,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);
  // ``cyState`` is the same instance as ``cyRef.current`` but tracked in
  // React state so child overlays re-render when cytoscape mounts.
  const [cyState, setCyState] = useState<cytoscape.Core | null>(null);
  // Bumped after layout/pan/zoom so the overlay reflows positions.
  const [layoutTick, setLayoutTick] = useState(0);

  // Pre-compute the area-error map so the renderer can mark failed
  // areas red without hunting through ``blob.areas`` per-element.
  const areaErrorById = useMemo(() => buildAreaErrorMap(blob.areas), [blob.areas]);

  useEffect(() => {
    let cancelled = false;
    let cy: cytoscape.Core | null = null;

    async function mount() {
      if (!containerRef.current) return;
      const cytoscapeModule = await import('cytoscape');
      const cytoscape = cytoscapeModule.default;
      // cytoscape-expand-collapse must register itself once per
      // cytoscape instance. The module's default export is the
      // ``register`` function in v4.
      const expandCollapseModule = await import('cytoscape-expand-collapse');
      const expandCollapse = (expandCollapseModule as { default: unknown })
        .default as (cy: typeof cytoscape) => void;
      try {
        expandCollapse(cytoscape);
      } catch {
        // The extension throws if already registered — safe to ignore.
      }
      if (cancelled || !containerRef.current) return;

      const elements = blobToCytoscapeElements(blob, areaErrorById, {
        highlightedEdgeId: highlightedEdgeId ?? null,
      });
      cy = cytoscape({
        container: containerRef.current,
        elements,
        layout: { name: 'preset' },
        style: [
          {
            selector: 'node',
            style: {
              label: 'data(label)',
              'font-size': 11,
              'text-valign': 'center',
              'text-halign': 'center',
              'background-color': '#1f2937',
              color: '#f9fafb',
              'border-width': 1,
              'border-color': '#374151',
              shape: 'round-rectangle',
              padding: '4px',
            },
          },
          {
            selector: ':parent',
            style: {
              'background-opacity': 0.06,
              'background-color': '#3b82f6',
              'border-color': '#1d4ed8',
              'border-width': 1,
              'text-valign': 'top',
              'text-halign': 'center',
              'font-weight': 'bold',
              padding: '12px',
            },
          },
          {
            selector: 'node[?failed]',
            style: {
              'border-color': '#ef4444',
              'border-width': 3,
              'background-color': '#fecaca',
              color: '#7f1d1d',
            },
          },
          {
            selector: 'edge',
            style: {
              width: 1.5,
              'curve-style': 'bezier',
              'target-arrow-shape': 'triangle',
              'line-color': 'data(color)',
              'target-arrow-color': 'data(color)',
            },
          },
          {
            selector: 'edge[?boundaryViolation]',
            style: {
              width: 2.5,
              'line-style': 'dashed',
              'line-color': VIOLATION_COLOUR,
              'target-arrow-color': VIOLATION_COLOUR,
            },
          },
          {
            selector: 'edge[?highlighted]',
            style: {
              width: 3.5,
              'line-color': VIOLATION_COLOUR,
              'target-arrow-color': VIOLATION_COLOUR,
            },
          },
          // Phase 7 P2 §11 — search controls.
          {
            selector: 'node.search-fade',
            style: { opacity: 0.2 },
          },
          {
            selector: 'node.search-match',
            style: { 'border-width': 3, 'border-color': '#facc15' },
          },
          // Phase 7 P2 §11 — edge-kind filter classes. One rule per
          // kind so toggling one doesn't reflow the others.
          {
            selector: 'edge.edge-kind-hidden-calls',
            style: { display: 'none' as const },
          },
          {
            selector: 'edge.edge-kind-hidden-imports',
            style: { display: 'none' as const },
          },
          {
            selector: 'edge.edge-kind-hidden-inherits',
            style: { display: 'none' as const },
          },
          {
            selector: 'edge.edge-kind-hidden-http',
            style: { display: 'none' as const },
          },
        ],
      });

      // Run the expand-collapse extension with all compounds initially
      // collapsed.
      const ec = (
        cy as unknown as {
          expandCollapse: (opts: {
            layoutBy: { name: string; padding?: number };
            fisheye: boolean;
            animate: boolean;
          }) => {
            collapseAll: () => void;
          };
        }
      ).expandCollapse({
        layoutBy: { name: 'cose', padding: 30 },
        fisheye: false,
        animate: false,
      });
      ec.collapseAll();
      cy.layout({ name: 'cose', padding: 30 }).run();
      cy.fit(undefined, 30);

      // Tap handlers — drive the side panel + evidence popover. They
      // forward into refs (captured per render) so handler identity
      // doesn't churn the cytoscape binding on each re-render.
      cy.on('tap', 'node', (evt) => {
        const id = evt.target.id() as string;
        onNodeClickRef.current?.(id);
      });
      cy.on('tap', 'edge', (evt) => {
        const id = evt.target.id() as string;
        const rendered = evt.renderedPosition ?? { x: 0, y: 0 };
        // Translate the cytoscape-local rendered position into
        // viewport (clientX/Y) coordinates so the portal-rendered
        // popover anchors correctly relative to ``document.body``.
        const rect = containerRef.current?.getBoundingClientRect();
        const pos = rect
          ? { x: rect.left + rendered.x, y: rect.top + rendered.y }
          : { x: rendered.x, y: rendered.y };
        onEdgeClickRef.current?.(id, pos);
      });

      // Bump layoutTick whenever the rendered geometry shifts so the
      // area-refresh overlay reflows.
      cy.on('layoutstop pan zoom resize', () => {
        setLayoutTick((t) => t + 1);
      });

      cyRef.current = cy;
      setCyState(cy);
      // Trigger one tick so the overlay computes initial positions
      // after the first layout completes.
      setLayoutTick((t) => t + 1);
    }

    mount();
    return () => {
      cancelled = true;
      if (cy) cy.destroy();
      cyRef.current = null;
      setCyState(null);
    };
  }, [blob, areaErrorById, highlightedEdgeId]);

  // Keep latest callback identity in a ref so the cytoscape tap binding
  // doesn't need to rebind every render.
  const onNodeClickRef = useRef(onNodeClick);
  const onEdgeClickRef = useRef(onEdgeClick);
  useEffect(() => {
    onNodeClickRef.current = onNodeClick;
  }, [onNodeClick]);
  useEffect(() => {
    onEdgeClickRef.current = onEdgeClick;
  }, [onEdgeClick]);

  // Phase 7 P2 §11 — search class diff. Runs whenever the query or the
  // cytoscape instance changes. The effect mutates classes in-place
  // because rebuilding the entire element set on every keystroke would
  // throw away the layout the user is staring at.
  useEffect(() => {
    const cy = cyState;
    if (!cy) return;
    const { matches, fades } = computeSearchClasses(blob, searchQuery ?? '');
    cy.batch(() => {
      cy.nodes().forEach((n) => {
        const id = n.id();
        n.toggleClass('search-match', matches.has(id));
        n.toggleClass('search-fade', fades.has(id));
      });
    });
  }, [cyState, blob, searchQuery]);

  // Phase 7 P2 §11 — edge-kind filter diff. Per-edge ``edge-kind-hidden-<kind>``
  // class drives the cytoscape ``display: none`` rule registered in
  // the style array. Re-checking a kind removes the class.
  useEffect(() => {
    const cy = cyState;
    if (!cy) return;
    const hidden = hiddenEdgeKinds ?? new Set<Edge['kind']>();
    cy.batch(() => {
      cy.edges().forEach((e) => {
        const kind = e.data('kind') as Edge['kind'] | undefined;
        for (const k of ['calls', 'imports', 'inherits', 'http'] as Edge['kind'][]) {
          e.toggleClass(`edge-kind-hidden-${k}`, kind === k && hidden.has(k));
        }
      });
    });
  }, [cyState, blob, hiddenEdgeKinds]);

  return (
    <div
      ref={containerRef}
      data-testid="code-graph-canvas"
      className={`relative h-[calc(100vh-260px)] min-h-[400px] w-full rounded-md border bg-background ${className ?? ''}`}
    >
      {repoId !== undefined && (
        <AreaRefreshOverlay
          repoId={repoId}
          blob={blob}
          cy={cyState}
          layoutTick={layoutTick}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pure helpers — kept module-level + exported so they're trivially
// unit-testable without DOM.
// ---------------------------------------------------------------------------

interface CyElement {
  data: Record<string, unknown>;
  classes?: string;
}

function buildAreaErrorMap(areas: AreaStatus[]): Record<string, string | null> {
  const m: Record<string, string | null> = {};
  for (const a of areas) {
    m[`area:${a.name}`] = a.status === 'failed' ? a.error ?? 'failed' : null;
  }
  return m;
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

  for (const n of blob.nodes as Node[]) {
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
    const id = `${e.source}->${e.target}:${e.kind}`;
    const isViolation = e.boundary_violation === true;
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
        boundaryViolation: isViolation ? 1 : undefined,
        violationReason: e.violation_reason ?? undefined,
        highlighted: highlightedEdgeId === id ? 1 : undefined,
      },
    });
  }

  return elements;
}

// ---------------------------------------------------------------------------
// Phase 7 P2 §11 — search classes.
// ---------------------------------------------------------------------------

export interface SearchClassPartition {
  /** Node ids that match the query (case-insensitive substring on label). */
  matches: Set<string>;
  /** Node ids that do NOT match and should be faded out. */
  fades: Set<string>;
}

/**
 * Compute the search partition for a given query.
 *
 * An empty / whitespace-only query yields empty sets — caller is
 * expected to clear all search classes in that case. Match logic is
 * case-insensitive substring against ``node.label``.
 */
export function computeSearchClasses(
  blob: RepoGraphBlob,
  query: string,
): SearchClassPartition {
  const trimmed = query.trim();
  if (trimmed.length === 0) {
    return { matches: new Set(), fades: new Set() };
  }
  const needle = trimmed.toLowerCase();
  const matches = new Set<string>();
  const fades = new Set<string>();
  for (const n of blob.nodes as Node[]) {
    const haystack = (n.label ?? '').toLowerCase();
    if (haystack.includes(needle)) {
      matches.add(n.id);
    } else {
      fades.add(n.id);
    }
  }
  return { matches, fades };
}
