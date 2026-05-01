import { api } from './api';
import type { MetricsResponse } from '@/types/api';

export async function getMetrics(days: number): Promise<MetricsResponse> {
  return api<MetricsResponse>(`/api/metrics?days=${days}`);
}
