'use client';
import { useEffect, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { getMetrics } from '@/lib/metrics';
import { useWS } from './useWS';
import type { MetricsResponse } from '@/types/api';

const REFRESH_TRIGGERS = new Set([
  'task.created',
  'task.classified',
  'task.start_planning',
  'task.plan_ready',
  'task.approved',
  'task.rejected',
  'task.start_coding',
  'task.review_complete',
  'task.ci_passed',
  'task.ci_failed',
  'task.failed',
  'task.cleanup',
  'task.deleted',
]);

// Trailing-debounced WS refresh: collapses bursts of task events into a single
// /api/metrics call, since the server-side `_avg_completion_time` is N+1.
export function useMetrics(days: number) {
  const qc = useQueryClient();
  const query = useQuery<MetricsResponse>({
    queryKey: ['metrics', days],
    queryFn: () => getMetrics(days),
    staleTime: 30_000,
  });

  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useWS('event', (e) => {
    if (!REFRESH_TRIGGERS.has(e.event_type)) return;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      qc.invalidateQueries({ queryKey: ['metrics'] });
    }, 1500);
  });

  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  return query;
}
