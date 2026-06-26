'use client';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getHealthLoop,
  listHealthFindings,
  resumeHealthLoop,
  startHealthLoop,
  stopHealthLoop,
  suppressHealthFinding,
  type HealthLoopStatus,
} from '@/lib/health-loop';

const healthLoopKeys = {
  status: (repoId: number | null) => ['health-loop', 'status', repoId] as const,
  findings: (repoId: number | null) => ['health-loop', 'findings', repoId] as const,
};

export function useHealthLoop(repoId: number | null) {
  return useQuery<HealthLoopStatus>({
    queryKey: healthLoopKeys.status(repoId),
    queryFn: () => getHealthLoop(repoId as number),
    enabled: repoId !== null,
    // The state advances server-side as the supervisor drains batches, so
    // poll while the panel is open to keep the status strip live.
    refetchInterval: 10_000,
  });
}

export function useHealthFindings(repoId: number | null) {
  return useQuery({
    queryKey: healthLoopKeys.findings(repoId),
    queryFn: () => listHealthFindings(repoId as number),
    enabled: repoId !== null,
    staleTime: 30_000,
  });
}

export function useHealthLoopActions(repoId: number) {
  const qc = useQueryClient();
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: healthLoopKeys.status(repoId) });
    qc.invalidateQueries({ queryKey: healthLoopKeys.findings(repoId) });
  };

  const start = useMutation({ mutationFn: () => startHealthLoop(repoId), onSuccess: invalidate });
  const stop = useMutation({ mutationFn: () => stopHealthLoop(repoId), onSuccess: invalidate });
  const resume = useMutation({ mutationFn: () => resumeHealthLoop(repoId), onSuccess: invalidate });
  const suppress = useMutation({
    mutationFn: (findingHash: string) => suppressHealthFinding(repoId, findingHash),
    onSuccess: invalidate,
  });

  return { start, stop, resume, suppress };
}
