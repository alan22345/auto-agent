'use client';
// Cytoscape compound graph renderer (ADR-016 §11, Phase 2).
//
// Nodes nest: area → file → class → function. All areas start
// collapsed; the cytoscape-expand-collapse extension provides the
// toggle UI when the user clicks. Edge colours are keyed off ``kind``
// (calls=blue, imports=grey, inherits=purple) — Phase 7 polishes this;
// here we ship a usable default.
//
// Failed areas (``AreaStatus.status === 'failed'``) get a red border
// and surface their error through the node's tooltip data so users see
// *why* an area's interior is missing.

import { useEffect, useMemo, useRef } from 'react';
import type cytoscape from 'cytoscape';
import type { RepoGraphBlob, AreaStatus, Edge, Node } from '@/types/api';

const EDGE_COLOUR: Record<string, string> = {
  calls: '#3b82f6',
  imports: '#9ca3af',
  inherits: '#a855f7',
  http: '#f97316',
};

interface Props {
  blob: RepoGraphBlob;
  className?: string;
}

export function GraphCanvas({ blob, className }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);

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

      const elements = blobToCytoscapeElements(blob, areaErrorById);
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

      cyRef.current = cy;
    }

    mount();
    return () => {
      cancelled = true;
      if (cy) cy.destroy();
      cyRef.current = null;
    };
  }, [blob, areaErrorById]);

  return (
    <div
      ref={containerRef}
      data-testid="code-graph-canvas"
      className={`relative h-[calc(100vh-260px)] min-h-[400px] w-full rounded-md border bg-background ${className ?? ''}`}
    />
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

export function blobToCytoscapeElements(
  blob: RepoGraphBlob,
  areaErrorById: Record<string, string | null>,
): CyElement[] {
  const elements: CyElement[] = [];

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
    elements.push({
      data: {
        id: `${e.source}->${e.target}:${e.kind}`,
        source: e.source,
        target: e.target,
        kind: e.kind,
        color: EDGE_COLOUR[e.kind] ?? '#9ca3af',
        snippet: e.evidence.snippet,
        evidenceFile: e.evidence.file,
        evidenceLine: e.evidence.line,
        sourceKind: e.source_kind,
      },
    });
  }

  return elements;
}
