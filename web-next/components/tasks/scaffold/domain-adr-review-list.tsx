'use client';
import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  useDomainAdrs,
  useSubmitDomainAdrVerdict,
} from '@/hooks/useScaffoldArtefacts';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { ApiError } from '@/lib/api';
import { cn } from '@/lib/utils';
import type { ScaffoldVerdict } from '@/lib/tasks';
import type { ScaffoldDomainAdrEntry } from '@/types/api';

// ADR-018 Phase C-gate — each domain ADR gets an independent verdict.
// The parent advances only when every domain has a non-``revise``
// verdict; until then the row stays open. We render every domain in an
// expandable card so the user can review them in any order.

interface VerdictView {
  verdict?: string | null;
  comments?: string | null;
  revise_count?: number | null;
}

function VerdictBadge({ approval }: { approval: VerdictView | null }) {
  if (!approval || !approval.verdict) {
    return (
      <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
        Pending
      </span>
    );
  }
  const cls =
    approval.verdict === 'approved'
      ? 'bg-emerald-500/20 text-emerald-600'
      : approval.verdict === 'revise'
        ? 'bg-amber-500/20 text-amber-700'
        : 'bg-destructive/20 text-destructive';
  return (
    <span
      className={cn(
        'rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide',
        cls,
      )}
    >
      {approval.verdict}
    </span>
  );
}

function DomainAdrRow({
  taskId,
  entry,
}: {
  taskId: number;
  entry: ScaffoldDomainAdrEntry;
}) {
  const submit = useSubmitDomainAdrVerdict();
  const [open, setOpen] = useState(false);
  const [comments, setComments] = useState('');
  const [submitting, setSubmitting] = useState<ScaffoldVerdict | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  async function onVerdict(verdict: ScaffoldVerdict) {
    if (submitting) return;
    if (
      (verdict === 'revise' || verdict === 'rejected') &&
      !comments.trim()
    ) {
      setLocalError(`Add a comment explaining why you ${verdict} this ADR.`);
      return;
    }
    setSubmitting(verdict);
    setLocalError(null);
    try {
      await submit.mutateAsync({
        taskId,
        domainSlug: entry.slug,
        verdict,
        comments,
      });
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

  // Once a verdict is in for this domain, hide the buttons — the user
  // can still re-open the row to view the ADR. ``revise`` is the only
  // verdict that re-runs the architect, but the next round emits a new
  // verdict file, so the row updates naturally over WS invalidation.
  const approval = entry.approval as VerdictView | null;
  const hasVerdict = !!approval?.verdict;

  return (
    <div className="rounded border bg-muted/30">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-muted"
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span className="text-[10px] font-mono text-muted-foreground">
          {String(entry.index).padStart(3, '0')}
        </span>
        <span className="flex-1 truncate text-sm font-medium">
          {entry.name || entry.slug}
        </span>
        <span className="text-[10px] text-muted-foreground">{entry.slug}</span>
        <VerdictBadge approval={approval} />
      </button>
      <div className={cn(!open && 'hidden')}>
        <div className="prose prose-sm max-h-[40vh] max-w-none overflow-y-auto border-t px-3 pb-3 pt-2 text-xs leading-relaxed">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {entry.markdown || '_ADR body unavailable._'}
          </ReactMarkdown>
        </div>
        {approval?.comments && (
          <div className="border-t bg-background/60 px-3 py-2 text-[11px] text-muted-foreground">
            <span className="font-medium">Last comments:</span> {approval.comments}
          </div>
        )}
        {!hasVerdict && (
          <div className="space-y-2 border-t bg-background/60 p-3">
            <Textarea
              placeholder="Comments (required for revise / reject)…"
              rows={2}
              value={comments}
              onChange={(e) => setComments(e.target.value)}
              className="min-h-[52px] text-xs"
              disabled={submitting !== null}
            />
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
        )}
      </div>
    </div>
  );
}

export function DomainAdrReviewList({ taskId }: { taskId: number }) {
  const query = useDomainAdrs(taskId);

  if (query.isLoading) {
    return (
      <div className="rounded border bg-muted/30 p-3 text-xs text-muted-foreground">
        Loading domain ADRs…
      </div>
    );
  }
  if (query.error) {
    return (
      <div className="rounded border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
        Domain ADRs unavailable —{' '}
        {query.error instanceof ApiError
          ? query.error.detail
          : query.error instanceof Error
            ? query.error.message
            : 'unknown error'}
      </div>
    );
  }
  const entries = query.data ?? [];
  if (entries.length === 0) {
    return (
      <div className="rounded border bg-muted/30 p-3 text-xs text-muted-foreground">
        The architect has not written any domain ADRs yet.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="rounded border bg-muted/30 px-3 py-2">
        <div className="text-sm font-medium">Domain ADRs — per-row review</div>
        <div className="text-[11px] text-muted-foreground">
          Approve, revise, or reject each domain independently. The scaffold
          will dispatch child trios only when every row has a non-revise
          verdict.
        </div>
      </div>
      {entries.map((e) => (
        <DomainAdrRow key={e.slug} taskId={taskId} entry={e} />
      ))}
    </div>
  );
}
