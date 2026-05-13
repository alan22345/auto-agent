'use client';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useWS } from './useWS';
import { getVerifyAttempts } from '@/lib/attempts';
import type { VerifyAttemptOut } from '@/types/api';

// Refresh on any verify lifecycle event for this task.
const REFRESH_TRIGGERS = new Set([
  'task.verify_started',
  'task.verify_passed',
  'task.verify_failed',
  'task.verify_skipped_no_runner',
]);

export function useVerifyAttempts(taskId: number | null) {
  const qc = useQueryClient();
  const query = useQuery<VerifyAttemptOut[]>({
    queryKey: ['verify-attempts', taskId],
    queryFn: () => (taskId ? getVerifyAttempts(taskId) : Promise.resolve([])),
    enabled: taskId !== null,
    staleTime: 5_000,
  });

  useWS('event', (e) => {
    if (!taskId || e.task_id !== taskId) return;
    if (!REFRESH_TRIGGERS.has(e.event_type)) return;
    qc.invalidateQueries({ queryKey: ['verify-attempts', taskId] });
  });

  return query;
}
