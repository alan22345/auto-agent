import { api } from './api';
import type {
  ArchitectAttemptOut,
  DecisionOut,
  TrioReviewAttemptOut,
} from '@/types/api';

export async function getArchitectAttempts(taskId: number): Promise<ArchitectAttemptOut[]> {
  return api<ArchitectAttemptOut[]>(`/api/tasks/${taskId}/architect-attempts`);
}

export async function getTrioReviewAttempts(taskId: number): Promise<TrioReviewAttemptOut[]> {
  return api<TrioReviewAttemptOut[]>(`/api/tasks/${taskId}/trio-review-attempts`);
}

export async function getDecisions(taskId: number): Promise<DecisionOut[]> {
  return api<DecisionOut[]>(`/api/tasks/${taskId}/decisions`);
}

export async function pauseTrio(taskId: number): Promise<{ ok: true }> {
  return api<{ ok: true }>(`/api/tasks/${taskId}/pause-trio`, { method: 'POST' });
}
