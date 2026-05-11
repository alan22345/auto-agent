import { api } from './api';

export type SecretKey = 'github_pat' | 'anthropic_api_key';

export interface SecretListResponse {
  keys: SecretKey[];
}

export interface SecretTestResponse {
  ok: boolean;
  detail: string;
}

export async function listSecrets(): Promise<SecretListResponse> {
  return api<SecretListResponse>('/api/me/secrets');
}

export async function setSecret(key: SecretKey, value: string): Promise<void> {
  await api<{ ok: true }>(`/api/me/secrets/${key}`, {
    method: 'PUT',
    body: JSON.stringify({ value }),
  });
}

export async function clearSecret(key: SecretKey): Promise<void> {
  await api<{ ok: true }>(`/api/me/secrets/${key}`, {
    method: 'PUT',
    body: JSON.stringify({ value: null }),
  });
}

export async function testSecret(key: SecretKey): Promise<SecretTestResponse> {
  return api<SecretTestResponse>(`/api/me/secrets/${key}/test`, {
    method: 'POST',
  });
}

export async function signup(
  email: string,
  password: string,
  display_name: string,
): Promise<{ user_id: number; email: string; verification_sent: boolean }> {
  return api('/api/auth/signup', {
    method: 'POST',
    body: JSON.stringify({ email, password, display_name }),
  });
}

export async function verifyEmail(token: string): Promise<{ ok: true; user_id: number; email: string }> {
  return api(`/api/auth/verify/${encodeURIComponent(token)}`);
}
