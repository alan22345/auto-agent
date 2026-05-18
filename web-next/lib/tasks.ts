import { api } from './api';
import type {
  GateArtefact,
  GateDecisionOut,
  ScaffoldArtefactMarkdown,
  ScaffoldDomainAdrEntry,
  ScaffoldDomainGrillQuestion,
  TaskData,
} from '@/types/api';

export type ModeOverride = 'freeform' | 'human_in_loop' | null;

export async function createTask(input: {
  title: string;
  description?: string;
  repo?: string;
  modeOverride?: ModeOverride;
}) {
  const body: Record<string, string | null> = { title: input.title };
  if (input.description) body.description = input.description;
  if (input.repo) body.repo_name = input.repo;
  // null means "inherit from repo" — only send the field when the user
  // actually overrode it, so backend validation stays strict.
  if (input.modeOverride === 'freeform' || input.modeOverride === 'human_in_loop') {
    body.mode_override = input.modeOverride;
  }
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

// ADR-015 §2 / §6 Phase 12 — design/plan approval gate + audit history.

export async function getGateArtefact(taskId: number): Promise<GateArtefact> {
  return api<GateArtefact>(`/api/tasks/${taskId}/gate-artefact`);
}

export async function approvePlan(
  taskId: number,
  verdict: 'approved' | 'rejected',
  comments = '',
): Promise<TaskData> {
  return api<TaskData>(`/api/tasks/${taskId}/approve-plan`, {
    method: 'POST',
    body: JSON.stringify({ verdict, comments }),
  });
}

export async function getGateHistory(taskId: number): Promise<GateDecisionOut[]> {
  return api<GateDecisionOut[]>(`/api/tasks/${taskId}/gate-history`);
}

// ---------------------------------------------------------------------------
// ADR-018 Stage 5 — scaffold gate endpoints. The three POSTs drive the
// SCAFFOLD parent's state machine (intent grill answer / root ADR verdict
// / per-domain verdict); the three GETs surface the markdown artefacts
// the user is being asked to review.
// ---------------------------------------------------------------------------

export type ScaffoldVerdict = 'approved' | 'revise' | 'rejected';

export async function getScaffoldIntent(
  taskId: number,
): Promise<ScaffoldArtefactMarkdown> {
  return api<ScaffoldArtefactMarkdown>(`/api/tasks/${taskId}/scaffold/intent`);
}

export async function getScaffoldRootAdr(
  taskId: number,
): Promise<ScaffoldArtefactMarkdown> {
  return api<ScaffoldArtefactMarkdown>(
    `/api/tasks/${taskId}/scaffold/root-adr`,
  );
}

export async function listScaffoldDomainAdrs(
  taskId: number,
): Promise<ScaffoldDomainAdrEntry[]> {
  return api<ScaffoldDomainAdrEntry[]>(
    `/api/tasks/${taskId}/scaffold/domain-adrs`,
  );
}

export async function submitIntentGrillAnswer(
  taskId: number,
  answer: string,
): Promise<TaskData> {
  return api<TaskData>(`/api/tasks/${taskId}/scaffold/intent-grill-answer`, {
    method: 'POST',
    body: JSON.stringify({ answer }),
  });
}

export async function submitRootAdrVerdict(
  taskId: number,
  verdict: ScaffoldVerdict,
  comments = '',
): Promise<TaskData> {
  return api<TaskData>(`/api/tasks/${taskId}/scaffold/root-adr-verdict`, {
    method: 'POST',
    body: JSON.stringify({ verdict, comments }),
  });
}

export async function submitDomainAdrVerdict(
  taskId: number,
  domainSlug: string,
  verdict: ScaffoldVerdict,
  comments = '',
): Promise<TaskData> {
  return api<TaskData>(`/api/tasks/${taskId}/scaffold/domain-adr-verdict`, {
    method: 'POST',
    body: JSON.stringify({ domain_slug: domainSlug, verdict, comments }),
  });
}

// ADR-018 Stage 8 — per-domain grill round. The domain-grill agent pauses
// on a question; the user answers via this endpoint; the parent's state
// machine transitions back to BUILDING_DOMAIN_ADRS and the driver resumes.

export async function getScaffoldDomainGrillQuestion(
  taskId: number,
  slug: string,
): Promise<ScaffoldDomainGrillQuestion> {
  const params = new URLSearchParams({ slug });
  return api<ScaffoldDomainGrillQuestion>(
    `/api/tasks/${taskId}/scaffold/domain-grill-question?${params.toString()}`,
  );
}

export async function submitDomainGrillAnswer(
  taskId: number,
  domainSlug: string,
  answer: string,
): Promise<TaskData> {
  return api<TaskData>(`/api/tasks/${taskId}/scaffold/domain-grill-answer`, {
    method: 'POST',
    body: JSON.stringify({ domain_slug: domainSlug, answer }),
  });
}
