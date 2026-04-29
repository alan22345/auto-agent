'use client';
import { useEffect, useRef } from 'react';
import { useTaskMessages } from '@/hooks/useTaskMessages';
import { cn } from '@/lib/utils';

const KIND_STYLES: Record<string, string> = {
  user: 'bg-card',
  system: 'bg-muted text-muted-foreground italic',
  event: 'bg-secondary text-muted-foreground text-xs font-mono',
  stream: 'bg-background text-muted-foreground text-xs font-mono opacity-80',
  error: 'bg-destructive/10 text-destructive',
};

export function ChatArea({ taskId }: { taskId: number | null }) {
  const entries = useTaskMessages(taskId);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.scrollTo({ top: ref.current.scrollHeight });
  }, [entries]);

  if (taskId === null) {
    return <div className="flex flex-1 items-center justify-center text-muted-foreground">Select a task</div>;
  }
  if (!entries.length) {
    return <div className="flex flex-1 items-center justify-center text-muted-foreground">No messages yet</div>;
  }
  return (
    <div ref={ref} className="flex-1 overflow-auto p-4">
      {entries.map((e, i) => (
        <div key={i} className={cn('mb-2 rounded p-3', KIND_STYLES[e.kind] || 'bg-card')}>
          {e.kind === 'user' && e.sender && (
            <div className="mb-1 text-xs uppercase tracking-wide text-muted-foreground">{e.sender}</div>
          )}
          <div className="whitespace-pre-wrap text-sm">{e.message}</div>
        </div>
      ))}
    </div>
  );
}
