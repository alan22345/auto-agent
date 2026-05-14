'use client';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { approvePlan, getGateArtefact, getGateHistory } from '@/lib/tasks';
import { useWS } from './useWS';
import type { GateArtefact, GateDecisionOut } from '@/types/api';

// ADR-015 §2 / §6 Phase 12 — gate approval + audit history hooks.
//
// The artefact endpoint is status-driven on the backend, so the UI just
// asks for "whatever's at the current gate" and gets back ``plan.md``
// (complex flow) or ``design.md`` (complex_large). The history endpoint
// returns every persisted decision so the audit panel can render
// user + standin decisions in one timeline.

export function useGateArtefact(taskId: number | null, enabled = true) {
  return useQuery<GateArtefact>({
    queryKey: ['gate-artefact', taskId],
    queryFn: () => {
      if (taskId === null) throw new Error('taskId is required');
      return getGateArtefact(taskId);
    },
    enabled: enabled && taskId !== null,
    staleTime: 30_000,
    retry: false,
  });
}

export function useApprovePlan() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      taskId,
      verdict,
      comments,
    }: {
      taskId: number;
      verdict: 'approved' | 'rejected';
      comments?: string;
    }) => approvePlan(taskId, verdict, comments ?? ''),
    onSuccess: (_data, { taskId }) => {
      // The task status flips; the artefact + history both refresh.
      qc.invalidateQueries({ queryKey: ['tasks'] });
      qc.invalidateQueries({ queryKey: ['task', taskId] });
      qc.invalidateQueries({ queryKey: ['gate-artefact', taskId] });
      qc.invalidateQueries({ queryKey: ['gate-history', taskId] });
    },
  });
}

// Refresh the audit panel whenever a standin or the user records a
// decision on the wire. The backend publishes ``standin.decision`` from
// both paths so we only need to watch one event type.
const GATE_DECISION_EVENT = 'standin.decision';

export function useGateHistory(taskId: number | null) {
  const qc = useQueryClient();
  const query = useQuery<GateDecisionOut[]>({
    queryKey: ['gate-history', taskId],
    queryFn: () => (taskId ? getGateHistory(taskId) : Promise.resolve([])),
    enabled: taskId !== null,
    staleTime: 5_000,
  });

  useWS('event', (e) => {
    if (!taskId || e.task_id !== taskId) return;
    if (e.event_type !== GATE_DECISION_EVENT) return;
    qc.invalidateQueries({ queryKey: ['gate-history', taskId] });
  });

  return query;
}
