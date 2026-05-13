'use client';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getArchitectAttempts,
  getTrioReviewAttempts,
  pauseTrio,
} from '@/lib/trio';
import { useWS } from './useWS';
import type { ArchitectAttemptOut, TrioReviewAttemptOut } from '@/types/api';

// Refresh when the architect logs reasoning / decisions or the trio advances.
const ARCHITECT_TRIGGERS = new Set([
  'task.trio_architect_attempt',
  'task.trio_phase_changed',
  'task.trio_checkpoint',
  'task.trio_started',
]);

const REVIEW_TRIGGERS = new Set([
  'task.trio_review_attempt',
  'task.trio_review_complete',
]);

export function useArchitectAttempts(taskId: number | null) {
  const qc = useQueryClient();
  const query = useQuery<ArchitectAttemptOut[]>({
    queryKey: ['architect-attempts', taskId],
    queryFn: () => (taskId ? getArchitectAttempts(taskId) : Promise.resolve([])),
    enabled: taskId !== null,
    staleTime: 5_000,
  });

  useWS('event', (e) => {
    if (!taskId || e.task_id !== taskId) return;
    if (!ARCHITECT_TRIGGERS.has(e.event_type)) return;
    qc.invalidateQueries({ queryKey: ['architect-attempts', taskId] });
  });

  return query;
}

export function useTrioReviewAttempts(taskId: number | null) {
  const qc = useQueryClient();
  const query = useQuery<TrioReviewAttemptOut[]>({
    queryKey: ['trio-review-attempts', taskId],
    queryFn: () => (taskId ? getTrioReviewAttempts(taskId) : Promise.resolve([])),
    enabled: taskId !== null,
    staleTime: 5_000,
  });

  useWS('event', (e) => {
    if (!taskId || e.task_id !== taskId) return;
    if (!REVIEW_TRIGGERS.has(e.event_type)) return;
    qc.invalidateQueries({ queryKey: ['trio-review-attempts', taskId] });
  });

  return query;
}

export function usePauseTrio() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (taskId: number) => pauseTrio(taskId),
    onSuccess: (_data, taskId) => {
      // Parent task status will flip to BLOCKED; refresh task lists and detail.
      qc.invalidateQueries({ queryKey: ['tasks'] });
      qc.invalidateQueries({ queryKey: ['task', taskId] });
    },
  });
}
