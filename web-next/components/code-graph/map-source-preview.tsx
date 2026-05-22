// LOD 3 — source preview panel for the focused step. Phase 4.
//
// Reads ``node.file`` + ``line_start`` + ``line_end`` from the graph
// blob, fetches a clamped window via GET /api/repos/{id}/graph/code,
// and renders it with line numbers. Reuses the same endpoint the Raw
// graph's NodeSidePanel uses, so backend changes are zero.
'use client';
import { useQuery } from '@tanstack/react-query';
import { getGraphCodePreview } from '@/lib/code-graph';
import type { Node as GraphNode } from '@/types/api';
import { codeGraphKeys } from '@/hooks/useCodeGraphConfigs';

interface Props {
  repoId: number;
  node: GraphNode | null;
}

export function MapSourcePreview({ repoId, node }: Props) {
  const previewable =
    node != null && node.file != null && node.line_start != null && node.line_end != null;
  const query = useQuery({
    queryKey: [
      ...codeGraphKeys.config(repoId),
      'code-preview',
      node?.file,
      node?.line_start,
      node?.line_end,
    ],
    queryFn: () => {
      if (!node || !node.file || node.line_start == null || node.line_end == null) {
        return Promise.reject(new Error('No source location'));
      }
      return getGraphCodePreview(repoId, {
        path: node.file,
        line_start: node.line_start,
        line_end: node.line_end,
      });
    },
    enabled: previewable,
  });

  if (!node) {
    return (
      <div
        data-testid="map-source-preview"
        className="flex h-full items-center justify-center p-8 text-xs text-muted-foreground"
      >
        Step no longer exists — drill out to a higher LOD.
      </div>
    );
  }

  if (!previewable) {
    return (
      <div
        data-testid="map-source-preview"
        className="flex h-full items-center justify-center p-8 text-xs text-muted-foreground"
      >
        No source location for this step.
      </div>
    );
  }

  return (
    <div
      data-testid="map-source-preview"
      className="flex h-full min-h-0 flex-col gap-2 overflow-hidden p-4"
    >
      <header className="flex items-baseline gap-2 text-xs">
        <span className="font-semibold">{node.label}</span>
        <span className="font-mono text-muted-foreground">
          {node.file}:{node.line_start}-{node.line_end}
        </span>
      </header>
      {query.isLoading ? (
        <p className="text-xs text-muted-foreground">Loading source…</p>
      ) : query.isError ? (
        <p role="alert" className="text-xs text-destructive">
          {query.error instanceof Error
            ? query.error.message
            : 'Failed to load source.'}
        </p>
      ) : query.data ? (
        <pre className="min-h-0 flex-1 overflow-auto rounded-md border bg-card/40 p-3 text-[11px] leading-relaxed">
          {renderWithLineNumbers(query.data.content, query.data.line_start)}
        </pre>
      ) : null}
    </div>
  );
}

function renderWithLineNumbers(content: string, startLine: number): string {
  const lines = content.split('\n');
  const maxNum = startLine + lines.length - 1;
  const width = String(maxNum).length;
  return lines
    .map((line, i) => `${String(startLine + i).padStart(width, ' ')}  ${line}`)
    .join('\n');
}
