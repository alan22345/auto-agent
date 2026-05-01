import { api } from './api';

export type Source = {
  url: string;
  title: string;
  summary: string;
  query: string;
};

export type MemoryHit = {
  entity: { id: string; name: string; type: string; tags: string[] };
  facts: Array<{ id: string; content: string; kind: string; source: string | null }>;
};

export type ToolCallStart = {
  type: 'tool_call_start';
  tool: string;
  args: Record<string, unknown>;
};

export type SearchEvent =
  | (ToolCallStart)
  | ({ type: 'source' } & Source)
  | ({ type: 'memory_hit' } & MemoryHit)
  | { type: 'text'; delta: string }
  | { type: 'done'; answer: string; input_tokens: number; output_tokens: number }
  | { type: 'error'; message: string };

export type SearchSession = {
  id: number;
  title: string;
  created_at: string;
  updated_at: string;
};

export type SearchMessage = {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  tool_events: SearchEvent[];
  truncated: boolean;
  input_tokens: number;
  output_tokens: number;
  created_at: string;
};

export type SearchSessionDetail = SearchSession & { messages: SearchMessage[] };

export const createSession = () =>
  api<SearchSession>('/api/search/sessions', { method: 'POST', body: '{}' });

export const listSessions = () =>
  api<SearchSession[]>('/api/search/sessions');

export const getSession = (id: number) =>
  api<SearchSessionDetail>(`/api/search/sessions/${id}`);

export const deleteSession = (id: number) =>
  api<{ ok: true }>(`/api/search/sessions/${id}`, { method: 'DELETE' });

export const renameSession = (id: number, title: string) =>
  api<SearchSession>(`/api/search/sessions/${id}`, {
    method: 'PATCH',
    body: JSON.stringify({ title }),
  });
