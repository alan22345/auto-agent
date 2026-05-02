import { api } from './api';
import type { TaskData } from '@/types/api';

export async function createTask(input: { title: string; description?: string; repo?: string }) {
  const body: Record<string, string> = { title: input.title };
  if (input.description) body.description = input.description;
  if (input.repo) body.repo_name = input.repo;
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
