'use client';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getScaffoldDomainGrillQuestion,
  getScaffoldIntent,
  getScaffoldRootAdr,
  listScaffoldDomainAdrs,
  submitDomainAdrVerdict,
  submitDomainGrillAnswer,
  submitIntentGrillAnswer,
  submitRootAdrVerdict,
  type ScaffoldVerdict,
} from '@/lib/tasks';
import { ApiError } from '@/lib/api';
import { useWS } from './useWS';
import type {
  ScaffoldArtefactMarkdown,
  ScaffoldDomainAdrEntry,
  ScaffoldDomainGrillQuestion,
} from '@/types/api';

// ADR-018 Stage 5 — hooks for the three scaffold artefact endpoints and
// the three gate-verdict mutations. The hooks mirror the
// ``useGateApproval`` pattern: a per-artefact query and a verdict
// mutation that invalidates the relevant queries on success.
//
// The artefact endpoints 404 when the file hasn't been written yet
// (e.g. intent.md before the agent runs); we swallow those quietly so
// the UI can render a "waiting on the agent" state instead of an error.

function notFoundIsOk(e: unknown): boolean {
  return e instanceof ApiError && e.status === 404;
}

export function useIntent(taskId: number | null, enabled = true) {
  return useQuery<ScaffoldArtefactMarkdown | null>({
    queryKey: ['scaffold-intent', taskId],
    queryFn: async () => {
      if (taskId === null) throw new Error('taskId is required');
      try {
        return await getScaffoldIntent(taskId);
      } catch (e) {
        if (notFoundIsOk(e)) return null;
        throw e;
      }
    },
    enabled: enabled && taskId !== null,
    staleTime: 15_000,
    retry: false,
  });
}

export function useRootAdr(taskId: number | null, enabled = true) {
  return useQuery<ScaffoldArtefactMarkdown | null>({
    queryKey: ['scaffold-root-adr', taskId],
    queryFn: async () => {
      if (taskId === null) throw new Error('taskId is required');
      try {
        return await getScaffoldRootAdr(taskId);
      } catch (e) {
        if (notFoundIsOk(e)) return null;
        throw e;
      }
    },
    enabled: enabled && taskId !== null,
    staleTime: 15_000,
    retry: false,
  });
}

export function useDomainAdrs(taskId: number | null, enabled = true) {
  return useQuery<ScaffoldDomainAdrEntry[]>({
    queryKey: ['scaffold-domain-adrs', taskId],
    queryFn: () => {
      if (taskId === null) throw new Error('taskId is required');
      return listScaffoldDomainAdrs(taskId);
    },
    enabled: enabled && taskId !== null,
    staleTime: 15_000,
    retry: false,
  });
}

// The standin.decision event fires from both user and PO-standin
// verdict paths (router emits it from every scaffold POST). We
// invalidate every scaffold query whenever it arrives so the panel
// reflects the latest verdict + state without a manual refresh.
const SCAFFOLD_EVENTS = new Set([
  'standin.decision',
  'task.status_changed',
]);

export function useScaffoldInvalidationOnWS(taskId: number | null) {
  const qc = useQueryClient();
  useWS('event', (e) => {
    if (!taskId || e.task_id !== taskId) return;
    if (!SCAFFOLD_EVENTS.has(e.event_type)) return;
    qc.invalidateQueries({ queryKey: ['scaffold-intent', taskId] });
    qc.invalidateQueries({ queryKey: ['scaffold-root-adr', taskId] });
    qc.invalidateQueries({ queryKey: ['scaffold-domain-adrs', taskId] });
    // ADR-018 Stage 8 — the domain grill question rotates per domain;
    // invalidate the whole sub-tree so whichever slug is active picks
    // up the new pending question or its disappearance.
    qc.invalidateQueries({ queryKey: ['scaffold-domain-grill-question', taskId] });
    qc.invalidateQueries({ queryKey: ['gate-history', taskId] });
    qc.invalidateQueries({ queryKey: ['task', taskId] });
    qc.invalidateQueries({ queryKey: ['tasks'] });
  });
}

export function useSubmitIntentGrillAnswer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      taskId,
      answer,
    }: {
      taskId: number;
      answer: string;
    }) => submitIntentGrillAnswer(taskId, answer),
    onSuccess: (_data, { taskId }) => {
      qc.invalidateQueries({ queryKey: ['scaffold-intent', taskId] });
      qc.invalidateQueries({ queryKey: ['tasks'] });
      qc.invalidateQueries({ queryKey: ['task', taskId] });
    },
  });
}

export function useSubmitRootAdrVerdict() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      taskId,
      verdict,
      comments,
    }: {
      taskId: number;
      verdict: ScaffoldVerdict;
      comments?: string;
    }) => submitRootAdrVerdict(taskId, verdict, comments ?? ''),
    onSuccess: (_data, { taskId }) => {
      qc.invalidateQueries({ queryKey: ['scaffold-root-adr', taskId] });
      qc.invalidateQueries({ queryKey: ['scaffold-domain-adrs', taskId] });
      qc.invalidateQueries({ queryKey: ['gate-history', taskId] });
      qc.invalidateQueries({ queryKey: ['tasks'] });
      qc.invalidateQueries({ queryKey: ['task', taskId] });
    },
  });
}

// ADR-018 Stage 8 — domain-grill round. The agent pauses on a question
// per domain; the user answers via the dedicated endpoint; the parent's
// state machine transitions back to BUILDING_DOMAIN_ADRS and the driver
// resumes the grill agent for the same domain.

export function useDomainGrillQuestion(
  taskId: number | null,
  slug: string | null,
  enabled = true,
) {
  return useQuery<ScaffoldDomainGrillQuestion | null>({
    queryKey: ['scaffold-domain-grill-question', taskId, slug],
    queryFn: async () => {
      if (taskId === null || !slug) throw new Error('taskId and slug required');
      try {
        return await getScaffoldDomainGrillQuestion(taskId, slug);
      } catch (e) {
        if (notFoundIsOk(e)) return null;
        throw e;
      }
    },
    enabled: enabled && taskId !== null && !!slug,
    staleTime: 5_000,
    retry: false,
  });
}

export function useSubmitDomainGrillAnswer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      taskId,
      domainSlug,
      answer,
    }: {
      taskId: number;
      domainSlug: string;
      answer: string;
    }) => submitDomainGrillAnswer(taskId, domainSlug, answer),
    onSuccess: (_data, { taskId, domainSlug }) => {
      qc.invalidateQueries({
        queryKey: ['scaffold-domain-grill-question', taskId, domainSlug],
      });
      qc.invalidateQueries({ queryKey: ['scaffold-domain-adrs', taskId] });
      qc.invalidateQueries({ queryKey: ['gate-history', taskId] });
      qc.invalidateQueries({ queryKey: ['tasks'] });
      qc.invalidateQueries({ queryKey: ['task', taskId] });
    },
  });
}

export function useSubmitDomainAdrVerdict() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      taskId,
      domainSlug,
      verdict,
      comments,
    }: {
      taskId: number;
      domainSlug: string;
      verdict: ScaffoldVerdict;
      comments?: string;
    }) =>
      submitDomainAdrVerdict(taskId, domainSlug, verdict, comments ?? ''),
    onSuccess: (_data, { taskId }) => {
      qc.invalidateQueries({ queryKey: ['scaffold-domain-adrs', taskId] });
      qc.invalidateQueries({ queryKey: ['gate-history', taskId] });
      qc.invalidateQueries({ queryKey: ['tasks'] });
      qc.invalidateQueries({ queryKey: ['task', taskId] });
    },
  });
}
