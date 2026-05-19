'use client';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  listRepoSecrets,
  putRepoSecret,
  clearRepoSecret,
  deleteRepoSecret,
  revealRepoSecret,
  testRepoSecret,
  recheckScaffoldSecrets,
} from '@/lib/repo-secrets';
import type { RepoSecretsResponse } from '@/lib/repo-secrets';

// ADR-019 §9 — TanStack Query hooks for per-repo secrets.

export const repoSecretsKeys = {
  list: (repoId: number) => ['repo-secrets', repoId] as const,
};

export function useRepoSecrets(repoId: number | null) {
  return useQuery<RepoSecretsResponse>({
    queryKey: repoSecretsKeys.list(repoId ?? 0),
    queryFn: () => listRepoSecrets(repoId!),
    enabled: repoId !== null && repoId > 0,
    staleTime: 15_000,
  });
}

export function useSetRepoSecret(repoId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ key, value }: { key: string; value: string | null }) =>
      putRepoSecret(repoId, key, value),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: repoSecretsKeys.list(repoId) });
    },
  });
}

export function useClearRepoSecret(repoId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => clearRepoSecret(repoId, key),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: repoSecretsKeys.list(repoId) });
    },
  });
}

export function useDeleteRepoSecret(repoId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => deleteRepoSecret(repoId, key),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: repoSecretsKeys.list(repoId) });
    },
  });
}

export function useRevealRepoSecret(repoId: number) {
  return useMutation({
    mutationFn: (key: string) => revealRepoSecret(repoId, key),
  });
}

export function useTestRepoSecret(repoId: number) {
  return useMutation({
    mutationFn: (key: string) => testRepoSecret(repoId, key),
  });
}

export function useRecheckScaffoldSecrets(taskId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => recheckScaffoldSecrets(taskId),
    onSuccess: () => {
      // Invalidate the task list and the specific task detail so the scaffold-
      // secrets banner disappears immediately when the gate unblocks.
      // NOTE: ['tasks'] is the root list key used by useTasks; tighten to a
      // tasksKeys helper if one is introduced in future.
      qc.invalidateQueries({ queryKey: ['tasks'] });
      qc.invalidateQueries({ queryKey: ['task', taskId] });
    },
  });
}
