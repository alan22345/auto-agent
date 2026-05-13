import { api } from './api';
import type { ReviewAttemptOut, VerifyAttemptOut } from '@/types/api';

export async function getVerifyAttempts(taskId: number): Promise<VerifyAttemptOut[]> {
  return api<VerifyAttemptOut[]>(`/api/tasks/${taskId}/verify-attempts`);
}

export async function getReviewAttempts(taskId: number): Promise<ReviewAttemptOut[]> {
  return api<ReviewAttemptOut[]>(`/api/tasks/${taskId}/review-attempts`);
}
