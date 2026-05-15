// Hook over /api/repos/{id}/graph/latest. Polls while no analysis has
// finished so the UI auto-updates after the user clicks Refresh.
'use client';
import { useQuery } from '@tanstack/react-query';
import { getLatestRepoGraph } from '@/lib/code-graph';
import { codeGraphKeys } from './useCodeGraphConfigs';
import type { LatestRepoGraphData } from '@/types/api';

const POLL_INTERVAL_MS = 3000;

export function useRepoGraph(repoId: number | null) {
  return useQuery<LatestRepoGraphData>({
    queryKey: [...codeGraphKeys.config(repoId), 'latest'],
    queryFn: () => {
      if (repoId === null) {
        // Type-narrowing escape — `enabled` blocks the actual call.
        return Promise.reject(new Error('repoId is null'));
      }
      return getLatestRepoGraph(repoId);
    },
    enabled: repoId !== null,
    // Poll only while no analysis row exists yet — once we have one,
    // the websocket / manual refresh button triggers further fetches.
    refetchInterval: (q) => {
      const data = q.state.data as LatestRepoGraphData | undefined;
      if (!data) return POLL_INTERVAL_MS;
      // No blob landed yet — keep polling so the first analysis lands
      // without a manual reload.
      if (!data.blob) return POLL_INTERVAL_MS;
      return false;
    },
  });
}
