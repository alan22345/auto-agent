'use client';
// Boundary-violations side panel (ADR-016 §7 — Phase 5).
//
// Renders a collapsible list of edges with ``boundary_violation === true``
// from a ``RepoGraphBlob``. Collapsed by default; the header shows a
// destructive badge with the violation count. When expanded each row is
// a click target — the parent page lifts ``highlightedEdgeId`` so the
// graph canvas can highlight the corresponding edge.
//
// Empty state: trust-building copy explaining how public-surface
// inference works so users don't read absence-of-violations as
// "analyser broken." Per ADR-016 §7 the convention-based inference is a
// deliberate v1 trade-off and the UX should call that out.

import { useMemo, useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ChevronRight } from 'lucide-react';
import type { Edge, Node, RepoGraphBlob } from '@/types/api';

interface Props {
  blob: RepoGraphBlob;
  highlightedEdgeId?: string | null;
  onSelectEdge?: (edgeId: string | null) => void;
}

interface ViolationRow {
  edgeId: string;
  sourceLabel: string;
  targetLabel: string;
  reason: string;
}

export function ViolationsPanel({
  blob,
  highlightedEdgeId,
  onSelectEdge,
}: Props) {
  const [open, setOpen] = useState(false);
  const violations = useMemo(() => findViolations(blob), [blob]);
  const count = violations.length;

  return (
    <div
      data-testid="violations-panel"
      className="rounded-md border bg-card/40"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls="violations-panel-body"
        className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left"
      >
        <span className="flex items-center gap-2 text-sm font-semibold">
          <ChevronRight
            size={14}
            className={`transition-transform ${open ? 'rotate-90' : ''}`}
          />
          Boundary violations
        </span>
        <Badge
          variant={count > 0 ? 'destructive' : 'secondary'}
          data-testid="violations-count"
        >
          {count}
        </Badge>
      </button>

      {open && (
        <div
          id="violations-panel-body"
          data-testid="violations-panel-body"
          className="border-t px-3 py-2 text-sm"
        >
          {count === 0 ? (
            <p className="text-xs text-muted-foreground">
              No boundary violations detected. Public-surface inferred from
              naming conventions; explicit rules from .auto-agent/graph.yml.
            </p>
          ) : (
            <ul className="space-y-1">
              {violations.map((v) => {
                const isActive = highlightedEdgeId === v.edgeId;
                return (
                  <li key={v.edgeId}>
                    <Button
                      variant="ghost"
                      size="sm"
                      data-testid="violation-row"
                      data-active={isActive ? 'true' : undefined}
                      onClick={() =>
                        onSelectEdge?.(isActive ? null : v.edgeId)
                      }
                      className={`h-auto w-full justify-start whitespace-normal break-all px-2 py-1 text-left text-xs font-normal ${
                        isActive ? 'bg-muted' : ''
                      }`}
                    >
                      <span className="font-mono">{v.sourceLabel}</span>
                      <span className="mx-1 text-muted-foreground">
                        &rarr;
                      </span>
                      <span className="font-mono">{v.targetLabel}</span>
                      <span className="ml-2 text-muted-foreground">
                        ({v.reason})
                      </span>
                    </Button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

export function violationEdgeId(edge: Edge): string {
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
