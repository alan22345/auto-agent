'use client';
import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  useRootAdr,
  useSubmitRootAdrVerdict,
} from '@/hooks/useScaffoldArtefacts';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { ApiError } from '@/lib/api';
import { useVerdictAction } from '@/hooks/useVerdictAction';
import type { ScaffoldVerdict } from '@/lib/tasks';

// ADR-018 Phase B-gate — the root ADR is the system-level design doc.
// Approve advances to per-domain ADR phase; revise sends it back to the
// architect (bounded at 3 rounds); reject blocks the parent.

export function RootAdrReviewCard({ taskId }: { taskId: number }) {
  const rootQuery = useRootAdr(taskId);
  const submit = useSubmitRootAdrVerdict();
  const [comments, setComments] = useState('');
  const [showComments, setShowComments] = useState(false);
  const { submitting, localError, runVerdict } = useVerdictAction();

  const onVerdict = async (verdict: ScaffoldVerdict) => {
    // ``revise`` is comments-required to be useful to the architect; if the
    // user clicks Revise/Reject without writing anything we surface the
    // comment box (via onMissingComment) and stop.
    const ok = await runVerdict(
      verdict,
      comments,
      () => submit.mutateAsync({ taskId, verdict, comments }),
      () => setShowComments(true),
    );
    if (ok) {
      setComments('');
      setShowComments(false);
    }
  };

  if (rootQuery.isLoading) {
    return (
      <div className="rounded border bg-muted/30 p-3 text-xs text-muted-foreground">
        Loading root ADR…
      </div>
    );
  }

  if (rootQuery.error) {
    return (
      <div className="rounded border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
        Root ADR unavailable —{' '}
        {rootQuery.error instanceof ApiError
          ? rootQuery.error.detail
          : rootQuery.error instanceof Error
            ? rootQuery.error.message
            : 'unknown error'}
      </div>
    );
  }

  const body = rootQuery.data?.markdown ?? '';
  if (!body.trim()) {
    return (
      <div className="rounded border bg-muted/30 p-3 text-xs text-muted-foreground">
        The architect has not written the root ADR yet.
      </div>
    );
  }

  return (
    <div className="rounded border bg-muted/30">
      <div className="border-b px-3 py-2">
        <div className="text-sm font-medium">
          Root ADR — Review and Approve
        </div>
        <div className="text-[11px] text-muted-foreground">
          The system-level design doc. Approve to start the per-domain ADRs;
          revise to send it back to the architect with comments.
        </div>
        <div className="mt-0.5 text-[10px] text-muted-foreground">
          <code>.auto-agent/adrs/000-system.md</code>
        </div>
      </div>
      <div className="prose prose-sm max-h-[50vh] max-w-none overflow-y-auto px-3 pb-3 pt-2 text-xs leading-relaxed">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
      </div>
      <div className="space-y-2 border-t bg-background/60 p-3">
        {(showComments ||
          comments.length > 0 ||
          submitting !== null) && (
          <Textarea
            placeholder="Comments (required for revise / reject)…"
            rows={3}
            value={comments}
            onChange={(e) => setComments(e.target.value)}
            className="min-h-[68px] text-xs"
            disabled={submitting !== null}
          />
        )}
        {localError && (
          <div className="text-xs text-destructive">{localError}</div>
        )}
        <div className="flex items-center justify-end gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => onVerdict('rejected')}
            disabled={submitting !== null}
          >
            {submitting === 'rejected' ? 'Rejecting…' : 'Reject'}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => onVerdict('revise')}
            disabled={submitting !== null}
          >
            {submitting === 'revise' ? 'Sending…' : 'Revise'}
          </Button>
          <Button
            size="sm"
            onClick={() => onVerdict('approved')}
            disabled={submitting !== null}
          >
            {submitting === 'approved' ? 'Approving…' : 'Approve'}
          </Button>
        </div>
      </div>
    </div>
  );
}
