import { api } from './api';
import type { GateArtefact, GateDecisionOut, TaskData } from '@/types/api';

export type ModeOverride = 'freeform' | 'human_in_loop' | null;

export async function createTask(input: {
  title: string;
  description?: string;
  repo?: string;
  modeOverride?: ModeOverride;
}) {
  const body: Record<string, string | null> = { title: input.title };
  if (input.description) body.description = input.description;
  if (input.repo) body.repo_name = input.repo;
  // null means "inherit from repo" — only send the field when the user
  // actually overrode it, so backend validation stays strict.
  if (input.modeOverride === 'freeform' || input.modeOverride === 'human_in_loop') {
    body.mode_override = input.modeOverride;
  }
  return api<TaskData>('/api/tasks', { method: 'POST', body: JSON.stringify(body) });
}
export async function markDone(id: number) {
  return api<TaskData>(`/api/tasks/${id}/done`, { method: 'POST' });
}
export async function cancelTask(id: number) {
  return api<TaskData>(`/api/tasks/${id}/cancel`, { method: 'POST' });
}
export async function deleteTask(id: number) {
  return api<{ ok: true }>(`/api/tasks/${id}`, { method: 'DELETE' });
}
export async function setPriority(id: number, priority: number) {
  return api<TaskData>(`/api/tasks/${id}/priority`, { method: 'POST', body: JSON.stringify({ priority }) });
}

// ADR-015 §2 / §6 Phase 12 — design/plan approval gate + audit history.

export async function getGateArtefact(taskId: number): Promise<GateArtefact> {
  return api<GateArtefact>(`/api/tasks/${taskId}/gate-artefact`);
}

export async function approvePlan(
  taskId: number,
  verdict: 'approved' | 'rejected',
  comments = '',
): Promise<TaskData> {
  return api<TaskData>(`/api/tasks/${taskId}/approve-plan`, {
    method: 'POST',
    body: JSON.stringify({ verdict, comments }),
  });
}

export async function getGateHistory(taskId: number): Promise<GateDecisionOut[]> {
  return api<GateDecisionOut[]>(`/api/tasks/${taskId}/gate-history`);
}
