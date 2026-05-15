'use client';
import { useQuery } from '@tanstack/react-query';
import { getRepoGraphConfig } from '@/lib/code-graph';
import { codeGraphKeys } from './useCodeGraphConfigs';
import type { RepoGraphConfigData } from '@/types/api';

export function useCodeGraphConfig(repoId: number | null) {
  return useQuery<RepoGraphConfigData | null>({
    queryKey: codeGraphKeys.config(repoId),
    queryFn: () => (repoId === null ? Promise.resolve(null) : getRepoGraphConfig(repoId)),
    enabled: repoId !== null,
    staleTime: 30_000,
  });
}
