'use client';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useWS } from './useWS';
import type { TaskData } from '@/types/api';

export function useTasks() {
  const qc = useQueryClient();
  const query = useQuery<TaskData[]>({
    queryKey: ['tasks'],
    queryFn: () => Promise.resolve(qc.getQueryData<TaskData[]>(['tasks']) || []),
    staleTime: Infinity,
  });

  useWS('task_list', (e) => qc.setQueryData<TaskData[]>(['tasks'], e.tasks));
  useWS('task_update', (e) => {
    qc.setQueryData<TaskData[]>(['tasks'], (prev) => {
      if (!prev) return [e.task];
      const i = prev.findIndex((t) => t.id === e.task.id);
      if (i === -1) return [e.task, ...prev];
      const next = prev.slice(); next[i] = e.task; return next;
    });
  });
  useWS('task_deleted', (e) => {
    qc.setQueryData<TaskData[]>(['tasks'], (prev) => (prev || []).filter((t) => t.id !== e.task_id));
  });

  return query;
}
