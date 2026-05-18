'use client';
// Edge evidence popover (ADR-016 §11 — Phase 7 P1c).
//
// Triggered by a cytoscape tap on an edge. Renders an overlay near the
// click position via a React portal so it can escape the canvas
// container's clipping. Closes on click-outside or Escape.
//
// Content rendered for every edge:
//   * kind chip (calls / imports / inherits / http)
//   * "<source.label> → <target.label>"
//   * the cited evidence ("<file>:<line>" + snippet)
//   * the source_kind tag (``ast`` or ``llm``)
//
// If ``boundary_violation === true`` the popover also surfaces the
// ``violation_reason`` in destructive styling.

import { useEffect, useMemo, useRef } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import type { Edge, Node as GraphNode, RepoGraphBlob } from '@/types/api';

interface Props {
  blob: RepoGraphBlob;
  edgeId: string;
  /** Pixel position inside the page (clientX/clientY-equivalent). The
   * popover floats near here. */
  position: { x: number; y: number };
  onClose: () => void;
}

export function EdgeEvidencePopover({
  blob,
  edgeId,
  position,
  onClose,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);

  const edge = useMemo(
    () => (blob.edges as Edge[]).find((e) => edgeKey(e) === edgeId) ?? null,
    [blob.edges, edgeId],
  );
  const labelById = useMemo(() => {
    const m = new Map<string, string>();
    for (const n of blob.nodes as GraphNode[]) m.set(n.id, n.label ?? n.id);
    return m;
  }, [blob.nodes]);

  // Click-outside / Escape — close on user dismiss.
  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (!ref.current) return;
      if (ref.current.contains(e.target as Node)) return;
      onClose();
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    // ``mousedown`` fires before the cytoscape tap (which is mouseup-
    // driven) so the next edge click will close + reopen cleanly.
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [onClose]);

  if (typeof document === 'undefined' || !edge) return null;

  const sourceLabel = labelById.get(edge.source) ?? edge.source;
  const targetLabel = labelById.get(edge.target) ?? edge.target;
  const isViolation = edge.boundary_violation === true;

  return createPortal(
    <div
      ref={ref}
      data-testid="edge-evidence-popover"
      role="dialog"
      aria-label="Edge evidence"
      style={{
        position: 'fixed',
        left: position.x,
        top: position.y,
        zIndex: 50,
      }}
      className="w-80 max-w-[90vw] -translate-x-1/2 rounded-md border bg-card p-3 text-xs shadow-lg"
    >
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <Badge
            variant={isViolation ? 'destructive' : 'secondary'}
            data-testid="edge-evidence-kind"
          >
            {edge.kind}
          </Badge>
          <Badge variant="outline" data-testid="edge-evidence-source-kind">
            {edge.source_kind}
          </Badge>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Close"
          data-testid="edge-evidence-close"
          onClick={() => onClose()}
          className="h-6 w-6"
        >
          <X size={12} />
        </Button>
      </div>

      <p
        data-testid="edge-evidence-endpoints"
        className="mb-2 break-all font-mono text-[11px]"
      >
        <span>{sourceLabel}</span>
        <span className="mx-1 text-muted-foreground">&rarr;</span>
        <span>{targetLabel}</span>
      </p>

      <div className="space-y-1">
        <p
          data-testid="edge-evidence-location"
          className="font-mono text-[11px] text-muted-foreground"
        >
          {edge.evidence.file}:{edge.evidence.line}
        </p>
        <pre
          data-testid="edge-evidence-snippet"
          className="overflow-auto rounded border bg-muted/40 p-2 font-mono text-[11px] leading-snug"
        >
          {edge.evidence.snippet}
        </pre>
      </div>

      {isViolation && edge.violation_reason && (
        <p
          role="alert"
          data-testid="edge-evidence-violation"
          className="mt-2 rounded border border-destructive/40 bg-destructive/10 p-2 text-[11px] text-destructive"
        >
          Boundary violation: {edge.violation_reason}
        </p>
      )}
    </div>,
    document.body,
  );
}

// Mirrors the canonical edge id used in graph-canvas + violations-panel.
function edgeKey(e: Edge): string {
  return `${e.source}->${e.target}:${e.kind}`;
}
