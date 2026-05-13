'use client';
import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useArchitectAttempts } from '@/hooks/useTrioArtifacts';
import { cn } from '@/lib/utils';
import type { ArchitectAttemptOut, ArchitectDecision } from '@/types/api';

const PHASE_LABEL: Record<ArchitectAttemptOut['phase'], string> = {
  initial: 'Initial',
  consult: 'Consult',
  checkpoint: 'Checkpoint',
  revision: 'Revision',
};

const PHASE_TONE: Record<ArchitectAttemptOut['phase'], string> = {
  initial: 'bg-blue-500/15 text-blue-700 dark:text-blue-400',
  consult: 'bg-amber-500/15 text-amber-700 dark:text-amber-400',
  checkpoint: 'bg-purple-500/15 text-purple-700 dark:text-purple-400',
  revision: 'bg-orange-500/15 text-orange-700 dark:text-orange-400',
};

function isArchitectDecision(d: unknown): d is ArchitectDecision {
  return (
    typeof d === 'object' &&
    d !== null &&
    typeof (d as { action?: unknown }).action === 'string'
  );
}

function ArchitectRow({ a }: { a: ArchitectAttemptOut }) {
  const decision = isArchitectDecision(a.decision) ? a.decision : null;
  return (
    <div className="border-l-2 pl-3 py-1.5">
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        <span
          className={cn(
            'rounded px-1.5 py-0.5 text-[10px] font-medium',
            PHASE_TONE[a.phase],
          )}
        >
          {PHASE_LABEL[a.phase]}
        </span>
        <span className="font-medium">Cycle {a.cycle}</span>
        {a.commit_sha && (
          <code className="text-[10px] text-muted-foreground">
            {a.commit_sha.slice(0, 7)}
          </code>
        )}
        <span className="ml-auto text-[10px] text-muted-foreground">
          {new Date(a.created_at).toLocaleString()}
        </span>
      </div>
      {a.consult_question && (
        <div className="mt-1 rounded bg-muted/40 px-2 py-1 text-[11px]">
          <div className="font-medium">Question</div>
          <div>{a.consult_question}</div>
          {a.consult_why && (
            <div className="mt-1 text-muted-foreground">
              Why: {a.consult_why}
            </div>
          )}
        </div>
      )}
      {a.reasoning && (
        <details className="mt-1 text-[11px]">
          <summary className="cursor-pointer text-muted-foreground">
            reasoning
          </summary>
          <pre className="mt-1 whitespace-pre-wrap text-[11px]">
            {a.reasoning}
          </pre>
        </details>
      )}
      {decision && (
        <div className="mt-1 text-[11px]">
          <span className="font-medium">Decision: </span>
          <span>{decision.action}</span>
          {decision.reason && (
            <span className="text-muted-foreground"> — {decision.reason}</span>
          )}
          {decision.question && (
            <div className="text-muted-foreground">
              Question: {decision.question}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function ArchitectAttemptsPanel({ taskId }: { taskId: number }) {
  const [open, setOpen] = useState(true);
  const { data = [], isLoading } = useArchitectAttempts(taskId);

  if (isLoading) {
    return (
      <div className="text-xs text-muted-foreground">Loading architect…</div>
    );
  }
  if (data.length === 0) {
    return (
      <div className="text-xs text-muted-foreground">
        No architect activity yet.
      </div>
    );
  }

  return (
    <div className="rounded border bg-muted/30">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1 px-2 py-1.5 text-left text-xs font-medium hover:bg-muted"
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span>Architect ({data.length})</span>
      </button>
      <div className={cn('space-y-1 px-3 pb-3 text-xs', !open && 'hidden')}>
        {data.map((a) => (
          <ArchitectRow key={a.id} a={a} />
        ))}
      </div>
    </div>
  );
}
