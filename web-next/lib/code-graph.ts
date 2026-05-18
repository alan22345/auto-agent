// Client for the code-graph API (ADR-016 Phase 2).
//
// Phase 1 exposed configuration CRUD; Phase 2 adds the live refresh
// endpoint (202 → REPO_GRAPH_REQUESTED event), the latest-analysis
// fetch the freshness banner reads, and the typed wire format
// (``RepoGraphBlob``).

import { api, ApiError } from './api';
import type {
  EnableRepoGraphRequest,
  GraphStalenessResponse,
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
//
// Phase 7 (ADR-016 §10): when ``area`` is supplied the analyser
// dispatches to the partial pipeline that re-runs analysis for only
// that area, merging the result into the existing graph blob.
export async function refreshRepoGraph(
  repoId: number,
  options: { area?: string } = {},
): Promise<RepoGraphRefreshResponse> {
  const qs = options.area
    ? `?area=${encodeURIComponent(options.area)}`
    : '';
  return api<RepoGraphRefreshResponse>(
    `/api/repos/${repoId}/graph/refresh${qs}`,
    { method: 'POST' },
  );
}

// Fetches a code preview window (ADR-016 §11 — Phase 7 side panel).
// The endpoint clamps the line range and refuses path-traversal; this
// helper exists so the React hook can typecheck.
export interface GraphCodePreviewResponse {
  file: string;
  line_start: number;
  line_end: number;
  content: string;
}

export async function getGraphCodePreview(
  repoId: number,
  params: { path: string; line_start: number; line_end: number },
): Promise<GraphCodePreviewResponse> {
  const qs = new URLSearchParams({
    path: params.path,
    line_start: String(params.line_start),
    line_end: String(params.line_end),
  });
  return api<GraphCodePreviewResponse>(
    `/api/repos/${repoId}/graph/code?${qs.toString()}`,
  );
}

// Fetch the latest completed analysis for a repo. ``blob`` is null
// when no analysis has finished yet.
export async function getLatestRepoGraph(repoId: number): Promise<LatestRepoGraphData> {
  return api<LatestRepoGraphData>(`/api/repos/${repoId}/graph/latest`);
}

// Compare the stored graph SHA against the analyser workspace HEAD
// (ADR-016 Phase 7 §11). Returns ``null`` when no graph exists to
// compare against (404) so the caller can treat "nothing to warn about"
// uniformly. Other errors propagate.
export async function getRepoGraphStaleness(
  repoId: number,
): Promise<GraphStalenessResponse | null> {
  try {
    return await api<GraphStalenessResponse>(
      `/api/repos/${repoId}/graph/staleness`,
    );
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

// The /code-graph onboarding modal needs to pick from existing repos —
// reuses the existing /api/repos endpoint.
export async function listRepos(): Promise<RepoData[]> {
  return api<RepoData[]>('/api/repos');
}
