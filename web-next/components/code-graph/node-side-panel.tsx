'use client';
// Node side panel (ADR-016 §11 — Phase 7 P1b).
//
// Opens when ``selectedNodeId`` is non-null on the parent page. Shows:
//   * a header with the node label, its source location, and a kind
//     badge,
//   * a lazily-loaded code preview window from
//     ``GET /api/repos/{repoId}/graph/code``,
//   * two collapsible sections (incoming / outgoing edges) grouped by
//     kind. Each row is clickable — selecting an edge bubbles up so
//     the parent page can highlight it in the canvas.

import { useMemo, useState } from 'react';
import { X, ChevronRight, ArrowUp, ArrowDown, Eraser } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { useNodeCodePreview } from '@/hooks/useNodeCodePreview';
import {
  computeAncestors,
  computeDescendants,
} from '@/lib/graph-reachability';
import type { Edge, Node, RepoGraphBlob } from '@/types/api';

interface Props {
  repoId: number;
  blob: RepoGraphBlob;
  nodeId: string;
  /** Bubble selected edge ids up so the parent page can wire them into
   * ``highlightedEdgeId`` on the canvas. */
  onSelectEdge?: (edgeId: string | null) => void;
  /** Called when the user dismisses the panel (X button). */
  onClose?: () => void;
  /** Phase 7 P2c §11 — emits the reachability subgraph (Set of node
   * ids) the canvas should highlight, or ``null`` to clear the
   * highlight. The parent page owns the canvas overlay state. */
  onHighlightReachability?: (nodes: Set<string> | null) => void;
  /** Open the edge-evidence popover for the given edge id. The parent
   * page anchors it relative to the row that fired the request so the
   * popover stays connected to the click target. ``null`` clears any
   * open popover. */
  onShowEdgeEvidence?: (
    edgeId: string,
    pos: { x: number; y: number },
  ) => void;
}

type ReachabilityMode = 'ancestors' | 'descendants' | null;

export function NodeSidePanel({
  repoId,
  blob,
  nodeId,
  onSelectEdge,
  onClose,
  onHighlightReachability,
  onShowEdgeEvidence,
}: Props) {
  // Local mirror of whether a reachability mode is active so the
  // "Clear highlight" button can render. The actual Set lives on the
  // parent (the canvas needs it); this state is just the toggle.
  const [reachabilityMode, setReachabilityMode] = useState<ReachabilityMode>(
    null,
  );
  const node = useMemo(
    () => (blob.nodes as Node[]).find((n) => n.id === nodeId) ?? null,
    [blob.nodes, nodeId],
  );

  // For compound nodes (``area`` / ``file``) the panel surfaces edges
  // that cross the node's subtree boundary — edges into a descendant
  // are "incoming", edges out of a descendant are "outgoing". For a
  // leaf node (function / class) the subtree set is ``{nodeId}`` and
  // the predicates degenerate to the plain ``source === nodeId`` /
  // ``target === nodeId`` match. Internal edges (both endpoints inside
  // the subtree) are deliberately hidden — they belong to the node's
  // interior, not its surface.
  const subtree = useMemo(
    () => collectSubtreeNodeIds(blob, nodeId),
    [blob, nodeId],
  );
  const incoming = useMemo(
    () =>
      groupEdgesByKind(
        blob.edges as Edge[],
        (e) => subtree.has(e.target) && !subtree.has(e.source),
      ),
    [blob.edges, subtree],
  );
  const outgoing = useMemo(
    () =>
      groupEdgesByKind(
        blob.edges as Edge[],
        (e) => subtree.has(e.source) && !subtree.has(e.target),
      ),
    [blob.edges, subtree],
  );

  // Code preview is only fetched when the node actually has a file +
  // line span. Area / module nodes (no file) just render without it.
  const preview = useNodeCodePreview({
    repoId,
    path: node?.file ?? null,
    lineStart: node?.line_start ?? null,
    lineEnd: node?.line_end ?? null,
  });

  if (!node) {
    return null;
  }

  return (
    <aside
      data-testid="node-side-panel"
      aria-label={`Details for ${node.label}`}
      className="flex h-full w-full max-w-md flex-col border-l bg-card text-sm shadow-lg"
    >
      <header className="flex items-start justify-between gap-2 border-b p-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3
              data-testid="node-side-panel-label"
              className="truncate text-base font-semibold"
              title={node.label}
            >
              {node.label}
            </h3>
            <Badge
              variant="secondary"
              data-testid="node-side-panel-kind"
              className="shrink-0"
            >
              {node.kind}
            </Badge>
          </div>
          {node.file && (
            <p
              data-testid="node-side-panel-location"
              className="mt-1 truncate font-mono text-xs text-muted-foreground"
              title={`${node.file}:${node.line_start}-${node.line_end}`}
            >
              {node.file}
              {node.line_start != null && node.line_end != null
                ? `:${node.line_start}-${node.line_end}`
                : ''}
            </p>
          )}
          {node.kind === 'function' && node.cyclomatic != null && (
            <div
              data-testid="node-complexity"
              className="mt-1 flex flex-wrap gap-1"
            >
              <Badge variant="outline" className="text-[10px]">
                cyclomatic {node.cyclomatic}
              </Badge>
              {node.cognitive != null && (
                <Badge variant="outline" className="text-[10px]">
                  cognitive {node.cognitive}
                </Badge>
              )}
              {node.loc != null && (
                <Badge variant="outline" className="text-[10px]">
                  loc {node.loc}
                </Badge>
              )}
            </div>
          )}
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Close panel"
          data-testid="node-side-panel-close"
          onClick={() => onClose?.()}
        >
          <X size={16} />
        </Button>
      </header>

      <div className="flex-1 space-y-3 overflow-auto p-3">
        {/* ADR-016 Phase 7 P2c §11 — ancestor / descendant highlight
         * controls. The buttons emit the reachability Set upward; the
         * "Clear highlight" affordance only renders when a mode is
         * active so it isn't dead UI when nothing is highlighted. */}
        <div
          className="flex flex-wrap items-center gap-2"
          data-testid="reachability-controls"
        >
          <Button
            type="button"
            variant={reachabilityMode === 'ancestors' ? 'default' : 'outline'}
            size="sm"
            data-testid="reachability-ancestors"
            onClick={() => {
              const set = computeAncestors(blob, nodeId);
              setReachabilityMode('ancestors');
              onHighlightReachability?.(set);
            }}
          >
            <ArrowUp size={14} className="mr-1" />
            Highlight ancestors
          </Button>
          <Button
            type="button"
            variant={
              reachabilityMode === 'descendants' ? 'default' : 'outline'
            }
            size="sm"
            data-testid="reachability-descendants"
            onClick={() => {
              const set = computeDescendants(blob, nodeId);
              setReachabilityMode('descendants');
              onHighlightReachability?.(set);
            }}
          >
            <ArrowDown size={14} className="mr-1" />
            Highlight descendants
          </Button>
          {reachabilityMode !== null && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              data-testid="reachability-clear"
              onClick={() => {
                setReachabilityMode(null);
                onHighlightReachability?.(null);
              }}
            >
              <Eraser size={14} className="mr-1" />
              Clear highlight
            </Button>
          )}
        </div>

        <CodePreview
          loading={preview.isLoading}
          error={preview.error}
          file={node.file ?? null}
          content={preview.data?.content ?? null}
        />

        <EdgeSection
          title="Incoming edges"
          testId="node-side-panel-incoming"
          groups={incoming}
          blob={blob}
          onSelect={onSelectEdge}
          onShowEvidence={onShowEdgeEvidence}
        />
        <EdgeSection
          title="Outgoing edges"
          testId="node-side-panel-outgoing"
          groups={outgoing}
          blob={blob}
          onSelect={onSelectEdge}
          onShowEvidence={onShowEdgeEvidence}
        />
      </div>
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Internal pieces
// ---------------------------------------------------------------------------

function CodePreview({
  loading,
  error,
  file,
  content,
}: {
  loading: boolean;
  error: unknown;
  file: string | null;
  content: string | null;
}) {
  if (file === null) {
    return (
      <div
        data-testid="node-side-panel-no-source"
        className="rounded border bg-muted/40 p-2 text-xs text-muted-foreground"
      >
        No source location for this node.
      </div>
    );
  }
  if (loading) {
    return (
      <div
        data-testid="node-side-panel-loading"
        className="rounded border bg-muted/40 p-2 text-xs text-muted-foreground"
      >
        Loading source…
      </div>
    );
  }
  if (error) {
    const message =
      error instanceof Error ? error.message : 'Failed to load preview.';
    return (
      <div
        role="alert"
        data-testid="node-side-panel-error"
        className="rounded border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive"
      >
        {message}
      </div>
    );
  }
  return (
    <pre
      data-testid="node-side-panel-code"
      className="max-h-72 overflow-auto rounded border bg-muted/40 p-2 font-mono text-xs leading-relaxed"
    >
      {content ?? ''}
    </pre>
  );
}

interface EdgeGroup {
  kind: Edge['kind'];
  edges: Edge[];
}

function EdgeSection({
  title,
  testId,
  groups,
  blob,
  onSelect,
  onShowEvidence,
}: {
  title: string;
  testId: string;
  groups: EdgeGroup[];
  blob: RepoGraphBlob;
  onSelect?: (edgeId: string | null) => void;
  onShowEvidence?: (edgeId: string, pos: { x: number; y: number }) => void;
}) {
  const [open, setOpen] = useState(true);
  const total = groups.reduce((acc, g) => acc + g.edges.length, 0);

  const labelById = useMemo(() => {
    const m = new Map<string, string>();
    for (const n of blob.nodes as Node[]) m.set(n.id, n.label ?? n.id);
    return m;
  }, [blob.nodes]);

  return (
    <section data-testid={testId} className="rounded border bg-card/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs font-semibold"
      >
        <span className="flex items-center gap-1">
          <ChevronRight
            size={12}
            className={`transition-transform ${open ? 'rotate-90' : ''}`}
          />
          {title}
        </span>
        <Badge
          variant={total > 0 ? 'secondary' : 'outline'}
          data-testid={`${testId}-count`}
        >
          {total}
        </Badge>
      </button>
      {open && (
        <div className="space-y-2 border-t px-3 py-2 text-xs">
          {total === 0 ? (
            <p className="text-muted-foreground">No edges in this direction.</p>
          ) : (
            groups.map((g) => (
              <div key={g.kind} data-testid={`${testId}-group-${g.kind}`}>
                <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                  {g.kind} ({g.edges.length})
                </p>
                <ul className="space-y-1">
                  {g.edges.map((e) => {
                    const id = `${e.source}->${e.target}:${e.kind}`;
                    return (
                      <li key={id}>
                        <button
                          type="button"
                          data-testid="edge-row"
                          data-edge-id={id}
                          onClick={(evt) => {
                            onSelect?.(id);
                            if (onShowEvidence) {
                              // Anchor the popover at the right edge of
                              // the row so it floats just outside the
                              // side panel rather than under the user's
                              // cursor.
                              const rect = (
                                evt.currentTarget as HTMLElement
                              ).getBoundingClientRect();
                              onShowEvidence(id, {
                                x: rect.right,
                                y: rect.top,
                              });
                            }
                          }}
                          className="w-full rounded px-1 py-0.5 text-left hover:bg-muted"
                        >
                          <span className="font-mono">
                            {labelById.get(e.source) ?? e.source}
                          </span>
                          <span className="mx-1 text-muted-foreground">
                            &rarr;
                          </span>
                          <span className="font-mono">
                            {labelById.get(e.target) ?? e.target}
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))
          )}
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

/**
 * Walk the parent hierarchy from ``startId`` and return the set of
 * node ids under it (inclusive of ``startId``).
 *
 * Compound nodes (kind=area / kind=file) have descendants that point at
 * them via ``Node.parent``; leaf nodes have none, so the result is just
 * ``{startId}``. The side panel uses this to surface edges that cross
 * the boundary of the selected node's subtree — an area node's
 * "outgoing" edges are edges whose source is anywhere inside it but
 * whose target is outside.
 *
 * Cycles are impossible by construction (parent forms a tree), but the
 * walk uses a ``visited`` set anyway as a cheap safety belt against
 * malformed blobs.
 */
export function collectSubtreeNodeIds(
  blob: RepoGraphBlob,
  startId: string,
): Set<string> {
  const childrenByParent = new Map<string, string[]>();
  for (const n of blob.nodes as Node[]) {
    if (n.parent == null) continue;
    const list = childrenByParent.get(n.parent) ?? [];
    list.push(n.id);
    childrenByParent.set(n.parent, list);
  }
  const visited = new Set<string>([startId]);
  const stack: string[] = [startId];
  while (stack.length > 0) {
    const id = stack.pop()!;
    const children = childrenByParent.get(id);
    if (!children) continue;
    for (const c of children) {
      if (visited.has(c)) continue;
      visited.add(c);
      stack.push(c);
    }
  }
  return visited;
}

export function groupEdgesByKind(
  edges: Edge[],
  predicate: (e: Edge) => boolean,
): EdgeGroup[] {
  const order: Edge['kind'][] = ['calls', 'imports', 'inherits', 'http'];
  const bucket = new Map<Edge['kind'], Edge[]>();
  for (const e of edges) {
    if (!predicate(e)) continue;
    const list = bucket.get(e.kind) ?? [];
    list.push(e);
    bucket.set(e.kind, list);
  }
  return order
    .filter((k) => bucket.has(k))
    .map((k) => ({ kind: k, edges: bucket.get(k)! }));
}
