'use client';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useWS } from './useWS';
import { getReviewAttempts } from '@/lib/attempts';
import type { ReviewAttemptOut } from '@/types/api';

const REFRESH_TRIGGERS = new Set([
  'task.review_complete',
  'task.review_ui_check_started',
  'task.review_skipped_no_runner',
]);

export function useReviewAttempts(taskId: number | null) {
  const qc = useQueryClient();
  const query = useQuery<ReviewAttemptOut[]>({
    queryKey: ['review-attempts', taskId],
    queryFn: () => (taskId ? getReviewAttempts(taskId) : Promise.resolve([])),
    enabled: taskId !== null,
    staleTime: 5_000,
  });

  useWS('event', (e) => {
    if (!taskId || e.task_id !== taskId) return;
    if (!REFRESH_TRIGGERS.has(e.event_type)) return;
    qc.invalidateQueries({ queryKey: ['review-attempts', taskId] });
  });

  return query;
}
