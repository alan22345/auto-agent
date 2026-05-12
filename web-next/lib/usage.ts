import { api } from './api';

export interface PlanRead {
  id: number;
  name: string;
  max_concurrent_tasks: number;
  max_tasks_per_day: number;
  max_input_tokens_per_day: number;
  max_output_tokens_per_day: number;
}

export interface UsageSummary {
  plan: PlanRead;
  active_tasks: number;
  tasks_today: number;
  input_tokens_today: number;
  output_tokens_today: number;
}

export function fetchUsageSummary(): Promise<UsageSummary> {
  return api<UsageSummary>('/api/usage/summary');
}
