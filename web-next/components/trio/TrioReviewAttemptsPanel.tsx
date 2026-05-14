'use client';
import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useTrioReviewAttempts } from '@/hooks/useTrioArtifacts';
import { cn } from '@/lib/utils';
import type { TrioReviewAttemptOut } from '@/types/api';

function TrioReviewRow({ a }: { a: TrioReviewAttemptOut }) {
  return (
    <div
      className={cn(
        'border-l-2 pl-3 py-1.5',
        a.ok
          ? 'border-l-green-500/60'
          : 'border-l-red-500/60',
      )}
    >
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        <span className="font-medium">Cycle {a.cycle}</span>
        <span
          className={cn(
            'rounded px-1.5 py-0.5 text-[10px] font-medium',
            a.ok
              ? 'bg-green-600 text-white'
              : 'bg-red-600 text-white',
          )}
        >
          {a.ok ? 'ok' : 'reject'}
        </span>
        <span className="ml-auto text-[10px] text-muted-foreground">
          {new Date(a.created_at).toLocaleString()}
        </span>
      </div>
      {a.feedback && (
        <details className="mt-1 text-[11px]" open={!a.ok}>
          <summary className="cursor-pointer text-muted-foreground">
            feedback
          </summary>
          <pre className="mt-1 max-h-72 overflow-y-auto whitespace-pre-wrap break-words rounded border bg-background/60 p-2 text-[11px]">
            {a.feedback}
          </pre>
        </details>
      )}
    </div>
  );
}

export function TrioReviewAttemptsPanel({ taskId }: { taskId: number }) {
  const [open, setOpen] = useState(true);
  const { data = [], isLoading } = useTrioReviewAttempts(taskId);

  if (isLoading) {
    return (
      <div className="text-xs text-muted-foreground">
        Loading trio reviews…
      </div>
    );
  }
  if (data.length === 0) {
    return (
      <div className="text-xs text-muted-foreground">
        No trio review attempts yet.
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
        <span>Trio Review ({data.length})</span>
      </button>
      <div className={cn('space-y-1 px-3 pb-3 text-xs', !open && 'hidden')}>
        {data.map((a) => (
          <TrioReviewRow key={a.id} a={a} />
        ))}
      </div>
    </div>
  );
}
