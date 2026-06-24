// Client for the auto-heal loop API (code-graph health remediation).
//
// The loop drains code-graph health findings onto a long-lived cleanup
// branch — filing/fixing/verifying each batch behind three gates (CI +
// smoke + differential) without ever touching `main`. These endpoints
// toggle it and surface its status for the health tab.

import { api } from './api';

export type HealthLoopState = 'idle' | 'running' | 'paused';

export interface HealthLoopBatchItem {
  hash: string;
  title: string;
}

export interface HealthLoopStatus {
  enabled: boolean;
  state: HealthLoopState;
  cleanup_branch: string;
  batch_size: number;
  cleanup_pr_url: string | null;
  current_batch: HealthLoopBatchItem[];
  merged_count: number;
  parked_count: number;
  suppressed_count: number;
  remaining_count: number;
}

export interface HealthFinding {
  finding_hash: string;
  category: string;
  title: string;
  files: string[];
  severity: number;
  suppressed: boolean;
  addressed: boolean;
}

export async function getHealthLoop(repoId: number): Promise<HealthLoopStatus> {
  return api<HealthLoopStatus>(`/api/repos/${repoId}/health-loop`);
}

export async function listHealthFindings(repoId: number): Promise<HealthFinding[]> {
  return api<HealthFinding[]>(`/api/repos/${repoId}/health-loop/findings`);
}

export async function startHealthLoop(repoId: number): Promise<HealthLoopStatus> {
  return api<HealthLoopStatus>(`/api/repos/${repoId}/health-loop/start`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export async function stopHealthLoop(repoId: number): Promise<HealthLoopStatus> {
  return api<HealthLoopStatus>(`/api/repos/${repoId}/health-loop/stop`, { method: 'POST' });
}

export async function resumeHealthLoop(repoId: number): Promise<HealthLoopStatus> {
  return api<HealthLoopStatus>(`/api/repos/${repoId}/health-loop/resume`, { method: 'POST' });
}

export async function suppressHealthFinding(
  repoId: number,
  findingHash: string,
): Promise<HealthLoopStatus> {
  return api<HealthLoopStatus>(`/api/repos/${repoId}/health-loop/suppress`, {
    method: 'POST',
    body: JSON.stringify({ finding_hash: findingHash }),
  });
}
