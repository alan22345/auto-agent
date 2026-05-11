import { api } from './api';

export type ClaudeAuthStatus = 'paired' | 'expired' | 'never_paired';

export interface PairStatus {
  claude_auth_status: ClaudeAuthStatus;
  claude_paired_at: string | null;
}

export async function startPairing() {
  return api<{ pairing_id: string; authorize_url: string }>(
    '/api/claude/pair/start',
    { method: 'POST' },
  );
}

export async function submitPairCode(pairing_id: string, code: string) {
  return api<{ ok: true }>('/api/claude/pair/code', {
    method: 'POST',
    body: JSON.stringify({ pairing_id, code }),
  });
}

export async function getPairStatus() {
  return api<PairStatus>('/api/claude/pair/status');
}

export async function disconnectClaude() {
  return api<{ ok: true }>('/api/claude/pair/disconnect', { method: 'POST' });
}
