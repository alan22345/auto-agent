// DOM tiles for the capability/flow map (Phase 3+).
//
// The map canvas renders ~5-12 capabilities or ~3-10 flows per LOD —
// small enough that pure CSS grid is faster + more readable than a
// cytoscape instance, and it gives us full Tailwind control over the
// tile content (name, description, terminal/entry icons, sub-flow list).
'use client';
import { Workflow, ArrowRightCircle, AlertTriangle } from 'lucide-react';
import type { Capability, EntryPoint, Flow, FlowJsonBlob } from '@/types/api';

// Stable visual tag for the entry-point kind. Kept short so the badge
// fits inside the tile.
const ENTRY_KIND_LABEL: Record<EntryPoint['kind'], string> = {
  http: 'HTTP',
  queue: 'queue',
  cron: 'cron',
  cli: 'CLI',
};

const TERMINAL_KIND_LABEL: Record<Flow['terminal_kind'], string> = {
  response: 'response',
  queue_publish: 'queue publish',
  external_http: 'external HTTP',
  db_write: 'db write',
  none: 'no terminal',
};

interface CapabilityTileProps {
  capability: Capability;
  flowsById: Map<string, Flow>;
  onOpen: (capabilityId: string) => void;
}

export function CapabilityTile({
  capability,
  flowsById,
  onOpen,
}: CapabilityTileProps) {
  const flows = capability.flow_ids
    .map((id) => flowsById.get(id))
    .filter((f): f is Flow => f != null);
  const topFlows = flows.slice(0, 4);
  return (
    <button
      type="button"
      onDoubleClick={() => onOpen(capability.id)}
      onClick={() => onOpen(capability.id)}
      data-testid={`capability-tile-${capability.id}`}
      className="group flex h-full min-h-[140px] flex-col items-stretch gap-2 rounded-lg border bg-card p-4 text-left transition hover:border-primary hover:bg-card/80 focus:outline-none focus:ring-2 focus:ring-primary"
    >
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-semibold leading-tight">
          {capability.name ?? capability.id}
        </h3>
        <span className="rounded-sm bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
          {flows.length} {flows.length === 1 ? 'flow' : 'flows'}
        </span>
      </div>
      {capability.description && (
        <p className="line-clamp-3 text-xs text-muted-foreground">
          {capability.description}
        </p>
      )}
      {topFlows.length > 0 && (
        <ul className="mt-auto space-y-0.5 text-[11px] text-muted-foreground">
          {topFlows.map((f) => (
            <li key={f.id} className="flex items-center gap-1 truncate">
              <Workflow size={10} className="shrink-0" />
              <span className="truncate">{f.name ?? f.id}</span>
            </li>
          ))}
          {flows.length > topFlows.length && (
            <li className="text-[10px] italic">
              +{flows.length - topFlows.length} more…
            </li>
          )}
        </ul>
      )}
    </button>
  );
}

interface FlowTileProps {
  flow: Flow;
  onOpen: (flowId: string) => void;
}

export function FlowTile({ flow, onOpen }: FlowTileProps) {
  return (
    <button
      type="button"
      onDoubleClick={() => onOpen(flow.id)}
      onClick={() => onOpen(flow.id)}
      data-testid={`flow-tile-${flow.id}`}
      className="group flex h-full min-h-[120px] flex-col items-stretch gap-2 rounded-lg border bg-card p-4 text-left transition hover:border-primary hover:bg-card/80 focus:outline-none focus:ring-2 focus:ring-primary"
    >
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-semibold leading-tight">
          {flow.name ?? flow.id}
        </h3>
        <span className="rounded-sm bg-amber-200/40 px-1.5 py-0.5 text-[10px] font-mono uppercase tracking-wide text-amber-900 dark:bg-amber-500/20 dark:text-amber-200">
          {ENTRY_KIND_LABEL[flow.entry_point.kind]}
        </span>
      </div>
      {flow.description && (
        <p className="line-clamp-3 text-xs text-muted-foreground">
          {flow.description}
        </p>
      )}
      <div className="mt-auto flex items-center gap-1 text-[11px] text-muted-foreground">
        <ArrowRightCircle size={11} />
        <span>{TERMINAL_KIND_LABEL[flow.terminal_kind]}</span>
        <span className="ml-auto rounded-sm bg-muted px-1 py-0.5 text-[10px]">
          {flow.steps.length} steps
        </span>
      </div>
    </button>
  );
}

interface MapEmptyProps {
  hasFlows: boolean;
  onCompute: () => void;
  computing: boolean;
  computeError: string | null;
}

export function MapEmpty({
  hasFlows,
  onCompute,
  computing,
  computeError,
}: MapEmptyProps) {
  if (hasFlows) {
    return (
      <div className="flex h-full min-h-[400px] flex-col items-center justify-center gap-2 rounded-md border bg-card/40 p-8 text-center">
        <AlertTriangle className="text-amber-500" />
        <h3 className="text-sm font-semibold">No entry points detected</h3>
        <p className="max-w-md text-xs text-muted-foreground">
          This repo has no HTTP routes, queue handlers, cron jobs, or CLI
          commands recognised by the Phase 1 heuristics. Use the{' '}
          <strong>Raw graph</strong> tab to inspect the underlying nodes.
        </p>
      </div>
    );
  }
  return (
    <div className="flex h-full min-h-[400px] flex-col items-center justify-center gap-3 rounded-md border bg-card/40 p-8 text-center">
      <Workflow className="text-primary" />
      <h3 className="text-sm font-semibold">No capability map yet</h3>
      <p className="max-w-md text-xs text-muted-foreground">
        Derive flows from the latest graph and label them with the LLM.
        This typically takes 5–30 seconds for medium repos.
      </p>
      <button
        type="button"
        onClick={onCompute}
        disabled={computing}
        className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
      >
        {computing ? 'Computing…' : 'Compute capability map'}
      </button>
      {computeError && (
        <p role="alert" className="text-xs text-destructive">
          {computeError}
        </p>
      )}
    </div>
  );
}
