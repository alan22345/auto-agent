// Hook over ``GET /api/repos/{id}/graph/staleness`` — ADR-016 Phase 7
// §11 freshness banner polish. Polls every 30s while ``enabled=true``
// so the banner notices when the analyser workspace drifts past the
// last analysed commit. The endpoint returns 404 when no analysis
// exists yet; the lib wrapper maps that to ``null`` so the banner can
// simply ignore the absence.
'use client';
import { useQuery } from '@tanstack/react-query';
import { getRepoGraphStaleness } from '@/lib/code-graph';
import { codeGraphKeys } from './useCodeGraphConfigs';
import type { GraphStalenessResponse } from '@/types/api';

const POLL_INTERVAL_MS = 30_000;

export function useRepoGraphStaleness(
  repoId: number | null,
  enabled: boolean = true,
) {
  return useQuery<GraphStalenessResponse | null>({
    queryKey: [...codeGraphKeys.config(repoId), 'staleness'],
    queryFn: () => {
      if (repoId === null) {
        return Promise.reject(new Error('repoId is null'));
      }
      return getRepoGraphStaleness(repoId);
    },
    enabled: repoId !== null && enabled,
    refetchInterval: POLL_INTERVAL_MS,
    // The endpoint is a cheap subprocess call but the banner shouldn't
    // re-render on every tab focus — let it ride the 30s poll.
    refetchOnWindowFocus: false,
  });
}
