// Hooks over /api/repos/{id}/graph/flows (Phase 3 capability/flow map).
//
// The query returns ``{ blob: FlowJsonBlob | null, ... }``. The mutation
// triggers a recompute and invalidates the query on success so the Map
// tab swaps from the empty state to the labelled view automatically.
'use client';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getRepoGraphFlows,
  recomputeRepoGraphFlows,
} from '@/lib/code-graph';
import { codeGraphKeys } from './useCodeGraphConfigs';
import type { LatestFlowsData } from '@/types/api';

const flowsKey = (repoId: number | null) => [
  ...codeGraphKeys.config(repoId),
  'flows',
];

export function useRepoGraphFlows(repoId: number | null) {
  return useQuery<LatestFlowsData>({
    queryKey: flowsKey(repoId),
    queryFn: () => {
      if (repoId === null) {
        return Promise.reject(new Error('repoId is null'));
      }
      return getRepoGraphFlows(repoId);
    },
    enabled: repoId !== null,
  });
}

export function useRecomputeRepoGraphFlows(repoId: number | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => {
      if (repoId === null) {
        return Promise.reject(new Error('repoId is null'));
      }
      return recomputeRepoGraphFlows(repoId);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: flowsKey(repoId) });
    },
  });
}
