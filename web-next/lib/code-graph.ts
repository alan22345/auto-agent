// Client for the code-graph API (ADR-016 Phase 2).
//
// Phase 1 exposed configuration CRUD; Phase 2 adds the live refresh
// endpoint (202 → REPO_GRAPH_REQUESTED event), the latest-analysis
// fetch the freshness banner reads, and the typed wire format
// (``RepoGraphBlob``).

import { api, ApiError } from './api';
import type {
  EnableRepoGraphRequest,
  LatestRepoGraphData,
  RepoData,
  RepoGraphConfigData,
  RepoGraphRefreshResponse,
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

// Triggers a graph refresh. Returns the 202 envelope with a
// ``request_id`` the caller can correlate with the eventual READY or
// FAILED event on the websocket.
export async function refreshRepoGraph(
  repoId: number,
): Promise<RepoGraphRefreshResponse> {
  return api<RepoGraphRefreshResponse>(`/api/repos/${repoId}/graph/refresh`, {
    method: 'POST',
  });
}

// Fetch the latest completed analysis for a repo. ``blob`` is null
// when no analysis has finished yet.
export async function getLatestRepoGraph(repoId: number): Promise<LatestRepoGraphData> {
  return api<LatestRepoGraphData>(`/api/repos/${repoId}/graph/latest`);
}

// The /code-graph onboarding modal needs to pick from existing repos —
// reuses the existing /api/repos endpoint.
export async function listRepos(): Promise<RepoData[]> {
  return api<RepoData[]>('/api/repos');
}
