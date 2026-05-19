import { api } from './api';

// ADR-019 §2 — per-repo encrypted secret store HTTP client functions.

export interface RepoSecretEntry {
  key: string;
  is_set: boolean;
  source: 'user' | 'architect_required';
  purpose?: string | null;
  updated_at?: string | null;
}

export interface RepoSecretsResponse {
  keys: RepoSecretEntry[];
}

export interface PutSecretResponse {
  ok: boolean;
  cleared: boolean;
}

export interface TestSecretResponse {
  ok: boolean;
  kind: string;
  message: string;
}

export interface RevealSecretResponse {
  value: string | null;
}

export interface RecheckSecretsResponse {
  unblocked: boolean;
  missing: string[];
}

export function listRepoSecrets(repoId: number): Promise<RepoSecretsResponse> {
  return api<RepoSecretsResponse>(`/api/repos/${repoId}/secrets`);
}

export function putRepoSecret(
  repoId: number,
  key: string,
  value: string | null,
): Promise<PutSecretResponse> {
  return api<PutSecretResponse>(`/api/repos/${repoId}/secrets/${encodeURIComponent(key)}`, {
    method: 'PUT',
    body: JSON.stringify({ value }),
  });
}

export function clearRepoSecret(repoId: number, key: string): Promise<PutSecretResponse> {
  return putRepoSecret(repoId, key, null);
}

export function deleteRepoSecret(repoId: number, key: string): Promise<void> {
  return api<void>(`/api/repos/${repoId}/secrets/${encodeURIComponent(key)}`, {
    method: 'DELETE',
  });
}

export function revealRepoSecret(repoId: number, key: string): Promise<RevealSecretResponse> {
  return api<RevealSecretResponse>(
    `/api/repos/${repoId}/secrets/${encodeURIComponent(key)}/reveal`,
    { method: 'POST' },
  );
}

export function testRepoSecret(repoId: number, key: string): Promise<TestSecretResponse> {
  return api<TestSecretResponse>(
    `/api/repos/${repoId}/secrets/${encodeURIComponent(key)}/test`,
    { method: 'POST' },
  );
}

export function recheckScaffoldSecrets(taskId: number): Promise<RecheckSecretsResponse> {
  return api<RecheckSecretsResponse>(`/api/scaffold/${taskId}/recheck-secrets`, {
    method: 'POST',
  });
}
