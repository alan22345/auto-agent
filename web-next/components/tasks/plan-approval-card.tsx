'use client';
import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useApprovePlan, useGateArtefact } from '@/hooks/useGateApproval';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { ApiError } from '@/lib/api';

// ADR-015 §2 Phase 12 — design/plan approval surface.
//
// The design doc is the single approval artefact for the whole
// complex_large run (§2); the same surface is reused for the complex
// flow's plan.md. The card renders the markdown body, an optional
// comments textarea, and Approve / Reject buttons; on submit it POSTs
// to /api/tasks/:id/approve-plan which writes plan_approval.json and
// advances the state machine.

type GateKind = 'plan' | 'design';

const HEADINGS: Record<GateKind, { title: string; sub: string }> = {
  plan: {
    title: 'Plan — Review and Approve',
    sub: 'Read the plan, then approve to start coding or reject to send it back.',
  },
  design: {
    title: 'Design — Review and Approve',
    sub: 'Read the architect\'s design. Approving emits the backlog; rejecting blocks the task.',
  },
};

export function PlanApprovalCard({ taskId }: { taskId: number }) {
  const { data, isLoading, error } = useGateArtefact(taskId);
  const approve = useApprovePlan();
  const [comments, setComments] = useState('');
  const [submitting, setSubmitting] = useState<'approved' | 'rejected' | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  async function submit(verdict: 'approved' | 'rejected') {
    if (submitting) return;
    setSubmitting(verdict);
    setLocalError(null);
    try {
      await approve.mutateAsync({ taskId, verdict, comments });
      setComments('');
    } catch (e) {
      setLocalError(
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : 'Failed to record verdict',
      );
    } finally {
      setSubmitting(null);
    }
  }

  if (isLoading) {
    return (
      <div className="rounded border bg-muted/30 p-3 text-xs text-muted-foreground">
        Loading gate artefact…
      </div>
    );
  }
  if (error || !data) {
    return (
      <div className="rounded border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
        Gate artefact unavailable —{' '}
        {error instanceof ApiError
          ? error.detail
          : error instanceof Error
            ? error.message
            : 'unknown error'}
      </div>
    );
  }

  const heading = HEADINGS[data.kind];

  return (
    <div className="rounded border bg-muted/30">
      <div className="border-b px-3 py-2">
        <div className="text-sm font-medium">{heading.title}</div>
        <div className="text-[11px] text-muted-foreground">{heading.sub}</div>
        <div className="mt-0.5 text-[10px] text-muted-foreground">
          <code>{data.path}</code>
        </div>
      </div>
      <div className="prose prose-sm max-h-[50vh] max-w-none overflow-y-auto px-3 pb-3 pt-2 text-xs leading-relaxed">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.body}</ReactMarkdown>
      </div>
      <div className="space-y-2 border-t bg-background/60 p-3">
        <Textarea
          placeholder="Optional comments…"
          rows={3}
          value={comments}
          onChange={(e) => setComments(e.target.value)}
          className="min-h-[68px] text-xs"
          disabled={submitting !== null}
        />
        {localError && (
          <div className="text-xs text-destructive">{localError}</div>
        )}
        <div className="flex items-center justify-end gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => submit('rejected')}
            disabled={submitting !== null}
          >
            {submitting === 'rejected' ? 'Rejecting…' : 'Reject'}
          </Button>
          <Button
            size="sm"
            onClick={() => submit('approved')}
            disabled={submitting !== null}
          >
            {submitting === 'approved' ? 'Approving…' : 'Approve'}
          </Button>
        </div>
      </div>
    </div>
  );
}
