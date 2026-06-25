// Capability/flow map renderer — Phase 3 (LOD 0 + LOD 1).
//
// Sibling to ``graph-canvas.tsx``. Reads a labelled ``FlowJsonBlob`` and
// renders the four LODs of the capability/flow map (Phase 3 ships
// LOD 0 + LOD 1; Phase 4 extends with LOD 2 step chains + LOD 3 source
// preview + boundary ports; Phase 5 adds URL state and cross-fade
// transitions).
//
// The component is pure DOM (CSS grid + Tailwind tiles). Cytoscape was
// considered but the per-LOD element count is small (~5-12 capabilities
// or ~3-10 flows) and the tile content is rich (description + flow list
// + entry/terminal badges), which is far easier to express in JSX +
// Tailwind than in cytoscape labels with DOM-sibling overlays. The spec
// (§7) describes a cytoscape implementation; this realisation keeps the
// same product surface (LODs, drill-in, ports, cross-fade) without the
// layout/style indirection.
'use client';
import { useMemo, useCallback, useEffect, useState } from 'react';
import { ChevronRight, ArrowRight } from 'lucide-react';
import type {
  Capability,
  Flow,
  FlowJsonBlob,
  Node as GraphNode,
  RepoGraphBlob,
} from '@/types/api';
import { CapabilityTile, FlowTile } from './map-tiles';
import { StepChain } from './map-step-chain';
import { MapSourcePreview } from './map-source-preview';
import {
  computeCapabilityPorts,
  computeSiblingFlowPorts,
} from './map-boundary-ports';

// Encodes the user's current focus position in the LOD tree. ``null``
// values bubble up to the appropriate parent LOD: only ``capabilityId``
// set => LOD 1; ``capabilityId`` + ``flowId`` set => LOD 2 (Phase 4);
// all three set => LOD 3 (Phase 4).
export interface FocusPath {
  capabilityId: string | null;
  flowId: string | null;
  stepNodeId: string | null;
}

export const ROOT_FOCUS: FocusPath = {
  capabilityId: null,
  flowId: null,
  stepNodeId: null,
};

type Lod = 0 | 1 | 2 | 3;

export function lodForFocus(focus: FocusPath): Lod {
  if (focus.stepNodeId) return 3;
  if (focus.flowId) return 2;
  if (focus.capabilityId) return 1;
  return 0;
}

// Drop the deepest non-null segment of a ``FocusPath`` — the in-component
// equivalent of the page-level ``drillOut`` exported from
// ``app/(app)/code-graph/[repoId]/page.tsx``. Duplicated to keep the
// page → canvas import direction one-way.
export function drillOutFocus(focus: FocusPath): FocusPath {
  if (focus.stepNodeId)
    return { capabilityId: focus.capabilityId, flowId: focus.flowId, stepNodeId: null };
  if (focus.flowId)
    return { capabilityId: focus.capabilityId, flowId: null, stepNodeId: null };
  return ROOT_FOCUS;
}

interface Props {
  blob: FlowJsonBlob;
  focus: FocusPath;
  onFocusChange: (next: FocusPath) => void;
  /** Optional search query — narrows the visible tile set at the
   * current LOD. Empty / whitespace = show everything. */
  searchQuery?: string;
  /** Repo id for LOD 3 source previews (Phase 4). */
  repoId: number;
  /** RepoGraphBlob — used to resolve step node ids to ``Node`` records
   * for LOD 2 step labels and LOD 3 source-preview targets. ``null`` is
   * tolerated for early renders before the latest graph load
   * settles. */
  graphBlob: RepoGraphBlob | null;
}

export function MapCanvas({
  blob,
  focus,
  onFocusChange,
  searchQuery,
  repoId,
  graphBlob,
}: Props) {
  const flowsById = useMemo(
    () => new Map<string, Flow>(blob.flows.map((f) => [f.id, f])),
    [blob.flows],
  );
  const capById = useMemo(
    () => new Map<string, Capability>(
      blob.capabilities.map((c) => [c.id, c]),
    ),
    [blob.capabilities],
  );
  const nodesById = useMemo(
    () =>
      new Map<string, GraphNode>(
        (graphBlob?.nodes ?? []).map((n) => [n.id, n]),
      ),
    [graphBlob?.nodes],
  );

  const lod = lodForFocus(focus);
  const focusedCapability = focus.capabilityId
    ? capById.get(focus.capabilityId) ?? null
    : null;
  const focusedFlow = focus.flowId
    ? flowsById.get(focus.flowId) ?? null
    : null;

  // Phase 5 §6 — keyboard nav. Esc / ArrowUp drills out one LOD, Home
  // returns to LOD 0. Lives on MapCanvas (not on the wrapping page)
  // so any host of the component gets the keybinds for free. Skip when
  // focus is inside a text input so typing isn't hijacked.
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      const target = ev.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.isContentEditable)
      ) {
        return;
      }
      if (ev.key === 'Escape' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        onFocusChange(drillOutFocus(focus));
        return;
      }
      if (ev.key === 'Home') {
        ev.preventDefault();
        onFocusChange(ROOT_FOCUS);
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [focus, onFocusChange]);

  // Search filter — case-insensitive substring on the visible field set
  // for the current LOD. Empty / whitespace clears the filter.
  const needle = (searchQuery ?? '').trim().toLowerCase();
  const filterCap = useCallback(
    (c: Capability) => {
      if (!needle) return true;
      return (
        (c.name ?? '').toLowerCase().includes(needle) ||
        (c.description ?? '').toLowerCase().includes(needle) ||
        c.id.toLowerCase().includes(needle)
      );
    },
    [needle],
  );
  const filterFlow = useCallback(
    (f: Flow) => {
      if (!needle) return true;
      return (
        (f.name ?? '').toLowerCase().includes(needle) ||
        (f.description ?? '').toLowerCase().includes(needle) ||
        f.id.toLowerCase().includes(needle) ||
        f.entry_point.node_id.toLowerCase().includes(needle)
      );
    },
    [needle],
  );

  // Phase 5 §7 — cross-fade between LODs. Re-keying the LOD wrapper on
  // focus-path changes triggers a React remount, which fires the
  // ``animate-in fade-in-0`` animation declared on the wrapper. Span is
  // ~300ms via Tailwind's ``duration-300`` utility.
  const lodSig = `lod-${lod}|${focus.capabilityId ?? ''}|${focus.flowId ?? ''}|${focus.stepNodeId ?? ''}`;
  const lodWrapperClass =
    'flex h-full flex-col animate-in fade-in-0 duration-300';

  if (lod === 0) {
    const caps = blob.capabilities.filter(filterCap);
    return (
      <div key={lodSig} data-testid="map-canvas" data-lod="0" className={lodWrapperClass}>
        <Breadcrumb focus={focus} onFocusChange={onFocusChange} capById={capById} flowsById={flowsById} />
        {caps.length === 0 ? (
          <NoMatches needle={needle} />
        ) : (
          <div className="grid flex-1 grid-cols-1 gap-3 overflow-auto p-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {caps.map((c) => (
              <CapabilityTile
                key={c.id}
                capability={c}
                flowsById={flowsById}
                onOpen={(capabilityId) =>
                  onFocusChange({
                    capabilityId,
                    flowId: null,
                    stepNodeId: null,
                  })
                }
              />
            ))}
          </div>
        )}
        {blob.unreached.length > 0 && (
          <UnreachedTray nodeIds={blob.unreached} />
        )}
      </div>
    );
  }

  if (lod === 1 && focusedCapability) {
    const flows = focusedCapability.flow_ids
      .map((id) => flowsById.get(id))
      .filter((f): f is Flow => f != null)
      .filter(filterFlow);
    const ports = computeCapabilityPorts(blob, focusedCapability);
    return (
      <div key={lodSig} data-testid="map-canvas" data-lod="1" className={lodWrapperClass}>
        <Breadcrumb focus={focus} onFocusChange={onFocusChange} capById={capById} flowsById={flowsById} />
        {focusedCapability.description && (
          <p className="px-2 pb-2 text-xs text-muted-foreground">
            {focusedCapability.description}
          </p>
        )}
        {flows.length === 0 ? (
          <NoMatches needle={needle} />
        ) : (
          <div className="grid flex-1 grid-cols-1 gap-3 overflow-auto p-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {flows.map((f) => (
              <FlowTile
                key={f.id}
                flow={f}
                onOpen={(flowId) =>
                  onFocusChange({
                    capabilityId: focus.capabilityId,
                    flowId,
                    stepNodeId: null,
                  })
                }
              />
            ))}
          </div>
        )}
        {ports.length > 0 && (
          <BoundaryPortsRow
            label="Linked to other capabilities"
            ports={ports.map((p) => ({
              key: p.capabilityId,
              label: p.capabilityName,
              detail: `${p.via.length} ${p.via.length === 1 ? 'call' : 'calls'}`,
              onOpen: () =>
                onFocusChange({
                  capabilityId: p.capabilityId,
                  flowId: null,
                  stepNodeId: null,
                }),
            }))}
          />
        )}
      </div>
    );
  }

  if (lod === 2 && focusedFlow && focusedCapability) {
    const ports = computeSiblingFlowPorts(
      blob,
      focusedCapability,
      focusedFlow,
    );
    return (
      <div key={lodSig} data-testid="map-canvas" data-lod="2" className={lodWrapperClass}>
        <Breadcrumb focus={focus} onFocusChange={onFocusChange} capById={capById} flowsById={flowsById} />
        {focusedFlow.description && (
          <p className="px-2 pb-1 text-xs text-muted-foreground">
            {focusedFlow.description}
          </p>
        )}
        <div className="min-h-0 flex-1 overflow-auto">
          <StepChain
            flow={focusedFlow}
            nodesById={nodesById}
            onOpenStep={(nodeId) =>
              onFocusChange({
                capabilityId: focus.capabilityId,
                flowId: focus.flowId,
                stepNodeId: nodeId,
              })
            }
          />
        </div>
        {ports.length > 0 && (
          <BoundaryPortsRow
            label="Also used in"
            ports={ports.map((p) => ({
              key: p.flowId,
              label: p.flowName,
              detail: `shares ${p.sharedNodeId}`,
              onOpen: () =>
                onFocusChange({
                  capabilityId: focus.capabilityId,
                  flowId: p.flowId,
                  stepNodeId: null,
                }),
            }))}
          />
        )}
      </div>
    );
  }
  if (lod === 3 && focusedFlow) {
    const node = focus.stepNodeId
      ? nodesById.get(focus.stepNodeId) ?? null
      : null;
    return (
      <div key={lodSig} data-testid="map-canvas" data-lod="3" className={lodWrapperClass}>
        <Breadcrumb focus={focus} onFocusChange={onFocusChange} capById={capById} flowsById={flowsById} />
        <div className="min-h-0 flex-1">
          <MapSourcePreview repoId={repoId} node={node} />
        </div>
      </div>
    );
  }

  // Defensive fallback — invalid focus path (stale URL pointing at a
  // capability/flow that no longer exists) lands here. Reset to LOD 0
  // rather than rendering empty.
  return (
    <div data-testid="map-canvas" data-lod="0" className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center">
      <p className="text-xs text-muted-foreground">
        Focus path no longer exists — returning to capabilities.
      </p>
      <button
        type="button"
        className="rounded-md border px-3 py-1.5 text-xs hover:bg-card"
        onClick={() => onFocusChange(ROOT_FOCUS)}
      >
        Back to capabilities
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Breadcrumb — segments collapse the focus path on click. Always visible
// at the top of the LOD so the user can drill out without keyboard.
// ---------------------------------------------------------------------------

interface BreadcrumbProps {
  focus: FocusPath;
  onFocusChange: (next: FocusPath) => void;
  capById: Map<string, Capability>;
  flowsById: Map<string, Flow>;
}

function Breadcrumb({
  focus,
  onFocusChange,
  capById,
  flowsById,
}: BreadcrumbProps) {
  const segments: { label: string; target: FocusPath }[] = [
    { label: 'Capabilities', target: ROOT_FOCUS },
  ];
  if (focus.capabilityId) {
    const cap = capById.get(focus.capabilityId);
    segments.push({
      label: cap?.name ?? focus.capabilityId,
      target: {
        capabilityId: focus.capabilityId,
        flowId: null,
        stepNodeId: null,
      },
    });
  }
  if (focus.flowId) {
    const flow = flowsById.get(focus.flowId);
    segments.push({
      label: flow?.name ?? focus.flowId,
      target: {
        capabilityId: focus.capabilityId,
        flowId: focus.flowId,
        stepNodeId: null,
      },
    });
  }
  if (focus.stepNodeId) {
    segments.push({
      label: focus.stepNodeId,
      target: focus,
    });
  }
  return (
    <nav
      data-testid="map-breadcrumb"
      aria-label="Map navigation"
      className="flex items-center gap-1 border-b px-2 py-2 text-xs"
    >
      {segments.map((seg, i) => {
        const isLast = i === segments.length - 1;
        return (
          <span key={i} className="flex items-center gap-1">
            {i > 0 && (
              <ChevronRight
                size={12}
                aria-hidden
                className="text-muted-foreground"
              />
            )}
            {isLast ? (
              <span className="font-medium" aria-current="page">
                {seg.label}
              </span>
            ) : (
              <button
                type="button"
                className="text-muted-foreground hover:text-foreground hover:underline"
                onClick={() => onFocusChange(seg.target)}
              >
                {seg.label}
              </button>
            )}
          </span>
        );
      })}
    </nav>
  );
}

function NoMatches({ needle }: { needle: string }) {
  return (
    <div
      data-testid="map-no-matches"
      className="flex flex-1 items-center justify-center p-8 text-xs text-muted-foreground"
    >
      {needle ? `No matches for "${needle}".` : 'Nothing to show.'}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Phase 5 §3 step 6 — Unreached tray at the bottom of LOD 0.
// ---------------------------------------------------------------------------

function UnreachedTray({ nodeIds }: { nodeIds: string[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div
      data-testid="unreached-tray"
      className="mt-2 border-t bg-card/40 px-2 py-2 text-xs"
    >
      <button
        type="button"
        className="flex items-center gap-1 text-muted-foreground hover:text-foreground"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <ChevronRight
          size={12}
          className={`transition-transform ${open ? 'rotate-90' : ''}`}
        />
        Unreached ({nodeIds.length} {nodeIds.length === 1 ? 'node' : 'nodes'})
      </button>
      {open && (
        <ul className="mt-2 grid max-h-40 grid-cols-1 gap-0.5 overflow-auto pl-4 text-[11px] sm:grid-cols-2 lg:grid-cols-3">
          {nodeIds.map((id) => (
            <li key={id} className="truncate font-mono text-muted-foreground">
              {id}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Boundary-ports row (Phase 4 §5). Pure DOM render of the cross-LOD ports
// computed by ``computeCapabilityPorts`` / ``computeSiblingFlowPorts``.
// Sits at the bottom of the LOD so users find linked capabilities/flows
// without having to scan the tile grid for badges.
// ---------------------------------------------------------------------------

interface BoundaryPort {
  key: string;
  label: string;
  detail: string;
  onOpen: () => void;
}

function BoundaryPortsRow({
  label,
  ports,
}: {
  label: string;
  ports: BoundaryPort[];
}) {
  return (
    <div
      data-testid="boundary-ports-row"
      className="mt-2 border-t bg-card/40 px-2 py-2 text-xs"
    >
      <span className="mr-2 text-[10px] uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <ul className="mt-1 flex flex-wrap gap-1.5">
        {ports.map((p) => (
          <li key={p.key}>
            <button
              type="button"
              onClick={p.onOpen}
              data-testid={`boundary-port-${p.key}`}
              className="flex items-center gap-1 rounded-md border bg-card px-2 py-1 text-[11px] hover:border-primary hover:bg-card/80"
            >
              <ArrowRight size={11} className="text-muted-foreground" />
              <span className="font-medium">{p.label}</span>
              <span className="text-[10px] text-muted-foreground">
                {p.detail}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
