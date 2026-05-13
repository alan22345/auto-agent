'use client';
import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useVerifyAttempts } from '@/hooks/useVerifyAttempts';
import { useReviewAttempts } from '@/hooks/useReviewAttempts';
import { cn } from '@/lib/utils';
import type { ReviewAttemptOut, VerifyAttemptOut } from '@/types/api';

type SubStatus = 'pass' | 'fail' | 'skipped' | string | null | undefined;

function StatusPill({ value, label }: { value: SubStatus; label: string }) {
  if (!value) return null;
  const tone =
    value === 'pass'
      ? 'bg-green-500/15 text-green-700 dark:text-green-400'
      : value === 'fail'
        ? 'bg-red-500/15 text-red-700 dark:text-red-400'
        : 'bg-muted text-muted-foreground';
  return (
    <span className={cn('rounded px-1.5 py-0.5 text-[10px] font-medium', tone)}>
      {label}: {value}
    </span>
  );
}

function OverallBadge({ status }: { status: string }) {
  const tone =
    status === 'pass'
      ? 'bg-green-600 text-white'
      : status === 'fail'
        ? 'bg-red-600 text-white'
        : 'bg-amber-500 text-white';
  return (
    <span className={cn('rounded px-1.5 py-0.5 text-[10px] font-medium', tone)}>
      {status}
    </span>
  );
}

function VerifyRow({ a }: { a: VerifyAttemptOut }) {
  return (
    <div className="border-l-2 pl-3 py-1.5">
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        <span className="font-medium">Cycle {a.cycle}</span>
        <OverallBadge status={a.status} />
        <StatusPill value={a.boot_check} label="boot" />
        <StatusPill value={a.intent_check} label="intent" />
      </div>
      {a.intent_judgment && (
        <pre className="mt-1 whitespace-pre-wrap text-[11px] text-muted-foreground">
          {a.intent_judgment}
        </pre>
      )}
      {a.failure_reason && (
        <div className="mt-1 text-[11px] text-red-600 dark:text-red-400">
          reason: {a.failure_reason}
        </div>
      )}
      {a.log_tail && (
        <details className="mt-1 text-[11px]">
          <summary className="cursor-pointer text-muted-foreground">
            server log tail
          </summary>
          <pre className="mt-1 whitespace-pre-wrap text-[10px]">{a.log_tail}</pre>
        </details>
      )}
    </div>
  );
}

function ReviewRow({ a }: { a: ReviewAttemptOut }) {
  return (
    <div className="border-l-2 pl-3 py-1.5">
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        <span className="font-medium">Cycle {a.cycle}</span>
        <OverallBadge status={a.status} />
        <StatusPill value={a.ui_check} label="ui" />
      </div>
      {a.code_review_verdict && (
        <details className="mt-1 text-[11px]" open={a.status !== 'pass'}>
          <summary className="cursor-pointer text-muted-foreground">
            code review
          </summary>
          <pre className="mt-1 whitespace-pre-wrap text-[11px]">
            {a.code_review_verdict}
          </pre>
        </details>
      )}
      {a.ui_judgment && (
        <details className="mt-1 text-[11px]" open={a.status !== 'pass'}>
          <summary className="cursor-pointer text-muted-foreground">
            ui judgment
          </summary>
          <pre className="mt-1 whitespace-pre-wrap text-[11px]">{a.ui_judgment}</pre>
        </details>
      )}
      {a.failure_reason && (
        <div className="mt-1 text-[11px] text-red-600 dark:text-red-400">
          reason: {a.failure_reason}
        </div>
      )}
      {a.log_tail && (
        <details className="mt-1 text-[11px]">
          <summary className="cursor-pointer text-muted-foreground">
            server log tail
          </summary>
          <pre className="mt-1 whitespace-pre-wrap text-[10px]">{a.log_tail}</pre>
        </details>
      )}
    </div>
  );
}

export function AttemptsPanel({ taskId }: { taskId: number }) {
  const [open, setOpen] = useState(true);
  const { data: verify = [] } = useVerifyAttempts(taskId);
  const { data: review = [] } = useReviewAttempts(taskId);

  if (verify.length === 0 && review.length === 0) return null;

  return (
    <div className="rounded border bg-muted/30">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1 px-2 py-1.5 text-left text-xs font-medium hover:bg-muted"
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span>
          Verify &amp; Review (
          {verify.length} verify, {review.length} review)
        </span>
      </button>
      <div
        className={cn(
          'space-y-2 px-3 pb-3 text-xs',
          !open && 'hidden',
        )}
      >
        {verify.length > 0 && (
          <div>
            <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
              Verify
            </div>
            {verify.map((a) => (
              <VerifyRow key={a.id} a={a} />
            ))}
          </div>
        )}
        {review.length > 0 && (
          <div>
            <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
              Review
            </div>
            {review.map((a) => (
              <ReviewRow key={a.id} a={a} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
