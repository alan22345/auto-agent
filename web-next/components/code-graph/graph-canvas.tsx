'use client';
// Cytoscape compound graph renderer (ADR-016 §11, Phase 2).
//
// Nodes nest: area → file → class → function. Edge colours are keyed
// off ``kind`` (calls=blue, imports=grey, inherits=purple). For graphs
// large enough to be unreadable when fully expanded (e.g. 3k+ nodes),
// the canvas starts with every area collapsed; the user expands areas
// individually via the cytoscape-expand-collapse ``+`` cues or the
// Collapse-all / Expand-all toolbar overlaid on the canvas.
//
// Layout uses ``fcose`` (cytoscape-fcose). cose was the original choice
// but failed to space compound subgraphs cleanly once the graph had
// more than ~50 nodes; fcose handles compound graphs natively and is
// deterministic with ``randomize:false`` so successive expand/collapse
// cycles don't shuffle the user's mental map.
//
// On collapse, edges of the same ``kind`` between the same two
// compounds bundle into a single meta-edge labelled ``kind (×N)`` —
// driven by the extension's ``groupEdgesOfSameTypeOnCollapse`` option
// with ``edgeTypeInfo: 'kind'`` so the meta-edge carries the kind on
// its data, and the ``expandcollapse.aftercollapseedge`` event hook
// derives ``label`` + ``color`` from that.
//
// Phase 5 (ADR-016 §7) overlays a destructive style on edges with
// ``boundary_violation === true``: thicker red dashed stroke that
// overrides the kind-based colour. The flag + reason are carried on
// the element data so a future side-panel can surface them.
//
// Phase 7 P3 (ADR-016 §11) overlays a dotted-line style on edges with
// ``source_kind === 'llm'``: the kind colour stays the same but a
// slimmer dotted stroke reads as "softer / less certain" than a
// tree-sitter-derived edge. Boundary violations still win when both
// flags coincide — the dashed red destructive overlay rule comes
// later in the style array.
//
// Failed areas (``AreaStatus.status === 'failed'``) get a red border
// and surface their error through the node's tooltip data so users see
// *why* an area's interior is missing.

import { useEffect, useMemo, useRef, useState } from 'react';
import type cytoscape from 'cytoscape';
import { ChevronsDownUp, ChevronsUpDown } from 'lucide-react';
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
  /** Phase 7 P2c — reachability subgraph (Set of node ids) the canvas
   * should highlight. Nodes inside the set get ``.reachability-highlight``;
   * everything else gets ``.reachability-fade``. ``null`` / undefined =
   * no overlay. */
  reachabilityHighlight?: Set<string> | null;
  /** Area names the user has chosen to hide. Every node whose ``area``
   * field matches gets ``display: none`` — that covers the area parent
   * + every descendant file / class / function. Edges with either
   * endpoint in a hidden area are also hidden so dangling stubs don't
   * litter the canvas. */
  hiddenAreas?: Set<string>;
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
  reachabilityHighlight,
  hiddenAreas,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);
  // Handle for the cytoscape-expand-collapse extension instance. Set
  // once on mount; used by the toolbar buttons.
  const expandCollapseApiRef = useRef<ExpandCollapseApi | null>(null);
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
      // fcose handles compound graphs cleanly and is deterministic with
      // ``randomize:false``. Required so that expand-collapse re-layouts
      // don't shuffle the rest of the graph.
      const fcoseModule = await import('cytoscape-fcose');
      const fcose = (fcoseModule as { default: unknown }).default;
      try {
        (cytoscape as unknown as { use: (ext: unknown) => void }).use(fcose);
      } catch {
        // Same idempotency note as expand-collapse: throws on second
        // registration in the same browser session.
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
          // Bundled meta-edge produced by the expand-collapse extension
          // when a compound is collapsed. ``data('label')`` is set by
          // the ``expandcollapse.aftercollapseedge`` handler from the
          // count of underlying edges.
          {
            selector: 'edge.cy-expand-collapse-collapsed-edge',
            style: {
              label: 'data(label)',
              'font-size': 9,
              color: '#1f2937',
              'text-background-color': '#ffffff',
              'text-background-opacity': 0.85,
              'text-background-padding': '2px',
              'text-background-shape': 'roundrectangle',
              width: 2,
            },
          },
          // Phase 7 §11 — AST vs LLM visual distinction. LLM-deduced
          // edges render dotted + slimmer so they read as "softer /
          // less certain" than tree-sitter-derived edges. The
          // boundary-violation rule below comes later and so wins when
          // both flags coincide (violation is the more urgent signal).
          {
            selector: 'edge[?sourceKindLlm]',
            style: {
              width: 1,
              'line-style': 'dotted',
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
          // 2026-05-21 — bulk per-area hide. Applied to nodes whose
          // ``area`` data matches a hidden area, and to edges whose
          // either endpoint is in a hidden area (so the canvas doesn't
          // sprout dangling stubs).
          {
            selector: 'node.area-hidden',
            style: { display: 'none' as const },
          },
          {
            selector: 'edge.area-hidden',
            style: { display: 'none' as const },
          },
          // Phase 7 P2c §11 — ancestor / descendant reachability
          // overlay. Fade is stronger than the search fade so the two
          // overlays compose readably (search inside a reachability
          // highlight still stands out).
          {
            selector: 'node.reachability-fade',
            style: { opacity: 0.15 },
          },
          {
            selector: 'node.reachability-highlight',
            style: { 'border-width': 3, 'border-color': '#22d3ee' },
          },
        ],
      });

      // Register the expand-collapse extension. ``layoutBy`` runs after
      // each toggle so the surrounding graph reflows around the new
      // compound size. ``edgeTypeInfo: 'kind'`` makes the extension
      // group edges of the same ``kind`` into a single meta-edge on
      // collapse — the label is decorated by the
      // ``aftercollapseedge`` handler below.
      const api = (
        cy as unknown as {
          expandCollapse: (opts: ExpandCollapseOptions) => ExpandCollapseApi;
        }
      ).expandCollapse({
        layoutBy: {
          name: 'fcose',
          randomize: false,
          animate: false,
          padding: 30,
        } as ExpandCollapseLayout,
        fisheye: false,
        animate: false,
        groupEdgesOfSameTypeOnCollapse: true,
        edgeTypeInfo: 'kind',
      });
      expandCollapseApiRef.current = api;

      // Decorate the meta-edge with a ``label`` + ``color`` derived
      // from its bundle so the existing ``data(label)`` / ``data(color)``
      // style selectors render correctly.
      const decorateCollapsedEdges = () => {
        cy!
          .edges('.cy-expand-collapse-collapsed-edge')
          .forEach((edge) => {
            const collapsed = edge.data('collapsedEdges');
            const count =
              collapsed && typeof collapsed.length === 'number'
                ? collapsed.length
                : 1;
            const kind = (edge.data('kind') as string | undefined) ?? '';
            edge.data('label', formatBundleLabel(kind, count));
            edge.data('color', EDGE_COLOUR[kind] ?? '#9ca3af');
          });
      };
      cy.on('expandcollapse.aftercollapseedge', decorateCollapsedEdges);

      // `cy.layout(...).run()` lays out asynchronously — fitting before
      // positions are final leaves nodes outside the viewport (canvas
      // looks empty on first render). Use the layout's documented
      // `stop` callback to fit *after* positions settle, with a
      // setTimeout backstop for the rare case where the layout never
      // fires `stop` (empty / single-element graph).
      const cyInstance = cy;
      const fitOnce = () => {
        try {
          cyInstance.fit(undefined, 30);
        } catch {
          // cy may already be destroyed during unmount race — silent.
        }
      };
      // Two-pass layout: run fcose with everything expanded so the
      // engine has good positions to fold into, then collapse-all,
      // then re-run fcose so the collapsed compounds get packed. cose
      // collapsed-all before initial layout left every node at (0,0);
      // fcose tolerates it better but the two-pass remains the safer
      // path documented in the handover.
      const collapseAndRelayout = () => {
        try {
          api.collapseAll();
        } catch {
          // Extension can throw if cytoscape was torn down during the
          // async layout window. Silent — the canvas is gone anyway.
        }
        try {
          cyInstance
            .layout({
              name: 'fcose',
              randomize: false,
              animate: false,
              padding: 30,
              stop: fitOnce,
            } as cytoscape.LayoutOptions)
            .run();
        } catch {
          // fcose registration races during unmount — silent.
        }
      };
      cyInstance
        .layout({
          name: 'fcose',
          randomize: false,
          animate: false,
          padding: 30,
          stop: collapseAndRelayout,
        } as cytoscape.LayoutOptions)
        .run();
      setTimeout(fitOnce, 500);

      // Tap handlers — drive the side panel + evidence popover. They
      // forward into refs (captured per render) so handler identity
      // doesn't churn the cytoscape binding on each re-render.
      cy.on('tap', 'node', (evt) => {
        const id = evt.target.id() as string;
        // Skip the synthesised expand-collapse cue nodes if any sneak
        // through — they have no id we care about.
        if (!id) return;
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

      // Zoom-aware auto-LOD was tried here and removed — it fought the
      // user during exploration (the canvas oscillated between
      // expanded and collapsed states as the zoom level crossed the
      // hysteresis thresholds during a layout reflow). The
      // Collapse-all / Expand-all toolbar + per-compound +/- cues +
      // per-area filter provide enough control without it.

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
      expandCollapseApiRef.current = null;
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

  // Phase 7 P2c §11 — reachability overlay diff. ``null`` (or
  // undefined) clears both classes; a non-empty Set highlights the
  // members and fades everything else.
  useEffect(() => {
    const cy = cyState;
    if (!cy) return;
    cy.batch(() => {
      if (!reachabilityHighlight) {
        cy.nodes().removeClass('reachability-highlight reachability-fade');
        return;
      }
      cy.nodes().forEach((n) => {
        const inSet = reachabilityHighlight.has(n.id());
        n.toggleClass('reachability-highlight', inSet);
        n.toggleClass('reachability-fade', !inSet);
      });
    });
  }, [cyState, reachabilityHighlight]);

  // 2026-05-21 — per-area hide diff. Mirrors the edge-kind handler
  // above but matches on ``data('area')`` so every descendant of the
  // hidden area also drops out.
  useEffect(() => {
    const cy = cyState;
    if (!cy) return;
    const hidden = hiddenAreas ?? new Set<string>();
    cy.batch(() => {
      cy.nodes().forEach((n) => {
        const area = n.data('area') as string | undefined;
        n.toggleClass('area-hidden', !!area && hidden.has(area));
      });
      cy.edges().forEach((e) => {
        const src = e.source();
        const tgt = e.target();
        const srcArea = src.data('area') as string | undefined;
        const tgtArea = tgt.data('area') as string | undefined;
        const hide =
          (!!srcArea && hidden.has(srcArea)) ||
          (!!tgtArea && hidden.has(tgtArea));
        e.toggleClass('area-hidden', hide);
      });
    });
  }, [cyState, blob, hiddenAreas]);

  const handleCollapseAll = () => {
    const cy = cyRef.current;
    const api = expandCollapseApiRef.current;
    if (!cy || !api) return;
    try {
      api.collapseAll();
      // Re-fit after the layout the extension queues internally; small
      // delay so we don't catch it mid-flight.
      setTimeout(() => {
        try {
          cy.fit(undefined, 30);
        } catch {
          /* unmounted */
        }
      }, 250);
    } catch {
      /* extension swallowed */
    }
  };

  const handleExpandAll = () => {
    const cy = cyRef.current;
    const api = expandCollapseApiRef.current;
    if (!cy || !api) return;
    try {
      api.expandAll();
      setTimeout(() => {
        try {
          cy.fit(undefined, 30);
        } catch {
          /* unmounted */
        }
      }, 250);
    } catch {
      /* extension swallowed */
    }
  };

  return (
    <div
      ref={containerRef}
      data-testid="code-graph-canvas"
      className={`relative h-[calc(100vh-260px)] min-h-[400px] w-full overflow-hidden rounded-md border bg-background ${className ?? ''}`}
    >
      {/* Toolbar overlay — child of the cytoscape host (same pattern as
        * ``AreaRefreshOverlay``). ``pointer-events-none`` on the wrapper
        * lets cytoscape capture every click on bare canvas; each
        * button opts back in with ``pointer-events-auto`` so it
        * intercepts its own click. Earlier attempts at making the
        * overlay a sibling of the host left cytoscape's internal
        * stacking context above the toolbar — clicks tunneled through
        * to graph nodes underneath. */}
      <div
        data-testid="graph-collapse-controls"
        className="pointer-events-none absolute right-2 top-2 z-30 flex items-center gap-1"
      >
        <button
          type="button"
          onClick={handleCollapseAll}
          data-testid="graph-collapse-all"
          aria-label="Collapse all areas"
          title="Collapse all areas"
          className="pointer-events-auto inline-flex h-7 items-center gap-1 rounded-md border bg-card/95 px-2 text-xs shadow-sm hover:bg-card"
        >
          <ChevronsDownUp size={12} />
          Collapse all
        </button>
        <button
          type="button"
          onClick={handleExpandAll}
          data-testid="graph-expand-all"
          aria-label="Expand all areas"
          title="Expand all areas"
          className="pointer-events-auto inline-flex h-7 items-center gap-1 rounded-md border bg-card/95 px-2 text-xs shadow-sm hover:bg-card"
        >
          <ChevronsUpDown size={12} />
          Expand all
        </button>
      </div>
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

// Local shapes for the cytoscape-expand-collapse extension. The package
// ships no .d.ts, so we type the surface we touch ourselves rather than
// litter the call sites with ``as any``.
interface ExpandCollapseLayout {
  name: string;
  randomize?: boolean;
  animate?: boolean;
  padding?: number;
}

interface ExpandCollapseOptions {
  layoutBy: ExpandCollapseLayout;
  fisheye: boolean;
  animate: boolean;
  groupEdgesOfSameTypeOnCollapse?: boolean;
  /** Data key whose value is used to group edges on collapse. We pass
   * ``'kind'`` so calls/imports/inherits/http each bundle separately. */
  edgeTypeInfo?: string;
}

interface ExpandCollapseApi {
  collapseAll: () => void;
  expandAll: () => void;
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

// ---------------------------------------------------------------------------
// 2026-05-21 — bundled-edge label format.
// ---------------------------------------------------------------------------

/**
 * Build the label rendered on a collapsed meta-edge.
 *
 * Singletons (count <= 1) render the kind alone — no ``(×1)`` noise.
 * Bundles render ``kind (×N)`` so the user sees both the relationship
 * and its weight at high zoom-out where the evidence popover is
 * impractical.
 */
export function formatBundleLabel(kind: string, count: number): string {
  if (count > 1) return `${kind} (×${count})`;
  return kind;
}
