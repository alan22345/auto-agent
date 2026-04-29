'use client';
import { useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useWS } from './useWS';
import { wsClient } from '@/lib/ws';
import type { TaskData } from '@/types/api';

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
  'task.clarification_needed',
  'task.subtask_progress',
  'task.deleted',
]);

export function useTasks() {
  const qc = useQueryClient();
  const query = useQuery<TaskData[]>({
    queryKey: ['tasks'],
    queryFn: () => Promise.resolve(qc.getQueryData<TaskData[]>(['tasks']) || []),
    staleTime: Infinity,
  });

  useEffect(() => {
    wsClient.send({ type: 'refresh' });
  }, []);

  useWS('task_list', (e) => qc.setQueryData<TaskData[]>(['tasks'], e.tasks));
  useWS('event', (e) => {
    if (REFRESH_TRIGGERS.has(e.event_type)) {
      wsClient.send({ type: 'refresh' });
    }
  });

  return query;
}
