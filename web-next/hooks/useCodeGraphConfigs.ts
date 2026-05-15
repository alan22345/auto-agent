'use client';
import { useQuery } from '@tanstack/react-query';
import { listRepoGraphConfigs, listRepos } from '@/lib/code-graph';
import type { RepoData, RepoGraphConfigData } from '@/types/api';

// Query keys exported so mutations can invalidate them centrally.
export const codeGraphKeys = {
  configs: ['code-graph', 'configs'] as const,
  config: (repoId: number | null) => ['code-graph', 'config', repoId] as const,
  repos: ['code-graph', 'repos'] as const,
};

export function useCodeGraphConfigs() {
  return useQuery<RepoGraphConfigData[]>({
    queryKey: codeGraphKeys.configs,
    queryFn: listRepoGraphConfigs,
    staleTime: 30_000,
  });
}

// Lists every repo for the current org — used by the onboarding modal
// to let the user pick which one to graph-enable.
export function useOrgRepos() {
  return useQuery<RepoData[]>({
    queryKey: codeGraphKeys.repos,
    queryFn: listRepos,
    staleTime: 60_000,
  });
}
