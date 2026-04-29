import { api } from './api';
import type { UserData } from '@/types/api';

export async function login(username: string, password: string) {
  return api<{ token: string; user: UserData }>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
}

export async function me() {
  return api<UserData>('/api/auth/me');
}

export async function logout() {
  return api<{ ok: true }>('/api/auth/logout', { method: 'POST' });
}
