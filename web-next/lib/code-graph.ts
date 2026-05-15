// Client for the code-graph API (ADR-016 Phase 1).
//
// The Phase 2 analyser will expose more endpoints (results, citations).
// Phase 1 surfaces only configuration CRUD plus the deliberate 501-stub
// refresh — see ``orchestrator/router.py::refresh_repo_graph``.

import { api, ApiError } from './api';
import type {
  EnableRepoGraphRequest,
  RepoData,
  RepoGraphConfigData,
  UpdateRepoGraphRequest,
} from '@/types/api';

export async function listRepoGraphConfigs(): Promise<RepoGraphConfigData[]> {
  return api<RepoGraphConfigData[]>('/api/graph/configs');
}

export async function getRepoGraphConfig(repoId: number): Promise<RepoGraphConfigData | null> {
  try {
    return await api<RepoGraphConfigData>(`/api/repos/${repoId}/graph`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

export async function enableRepoGraph(
  repoId: number,
  body: EnableRepoGraphRequest = {},
): Promise<RepoGraphConfigData> {
  return api<RepoGraphConfigData>(`/api/repos/${repoId}/graph`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function updateRepoGraphConfig(
  repoId: number,
  body: UpdateRepoGraphRequest,
): Promise<RepoGraphConfigData> {
  return api<RepoGraphConfigData>(`/api/repos/${repoId}/graph`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export async function disableRepoGraph(repoId: number): Promise<void> {
  await api<{ disabled: number }>(`/api/repos/${repoId}/graph`, {
    method: 'DELETE',
  });
}

// Deliberately throws a friendly error when Phase 2 hasn't shipped yet.
// Callers catch ApiError(501) and surface the message via a toast.
export async function refreshRepoGraph(repoId: number): Promise<void> {
  await api<unknown>(`/api/repos/${repoId}/graph/refresh`, { method: 'POST' });
}

// The /code-graph onboarding modal needs to pick from existing repos —
// reuses the existing /api/repos endpoint.
export async function listRepos(): Promise<RepoData[]> {
  return api<RepoData[]>('/api/repos');
}
