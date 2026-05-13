'use client';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { listRepos, updateProductBrief } from '@/lib/repos';
import type { RepoData } from '@/types/api';

export function useRepos() {
  return useQuery<RepoData[]>({
    queryKey: ['repos'],
    queryFn: listRepos,
    staleTime: 30_000,
  });
}

export function useUpdateProductBrief() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ repoId, brief }: { repoId: number; brief: string }) =>
      updateProductBrief(repoId, brief),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['repos'] });
    },
  });
}
