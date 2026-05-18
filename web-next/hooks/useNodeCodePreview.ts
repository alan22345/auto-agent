'use client';
// Lazy code-preview hook (ADR-016 §11 — Phase 7 side panel).
//
// Wraps ``GET /api/repos/{repoId}/graph/code`` so the side panel can
// fetch a clamped source window only when the user opens it. Caching
// is keyed on the (repoId, path, line range) tuple so re-clicking the
// same node hits the cache instantly.

import { useQuery } from '@tanstack/react-query';
import { getGraphCodePreview } from '@/lib/code-graph';
import type { GraphCodePreviewResponse } from '@/types/api';

interface Params {
  repoId: number;
  path: string | null;
  lineStart: number | null;
  lineEnd: number | null;
}

export function codePreviewKey({ repoId, path, lineStart, lineEnd }: Params) {
  return ['code-graph', 'code-preview', repoId, path, lineStart, lineEnd] as const;
}

export function useNodeCodePreview({
  repoId,
  path,
  lineStart,
  lineEnd,
}: Params) {
  return useQuery<GraphCodePreviewResponse>({
    queryKey: codePreviewKey({ repoId, path, lineStart, lineEnd }),
    queryFn: () => {
      if (path === null || lineStart === null || lineEnd === null) {
        return Promise.reject(new Error('code preview params missing'));
      }
      return getGraphCodePreview(repoId, {
        path,
        line_start: lineStart,
        line_end: lineEnd,
      });
    },
    enabled: path !== null && lineStart !== null && lineEnd !== null,
    staleTime: 5 * 60 * 1000,
  });
}
