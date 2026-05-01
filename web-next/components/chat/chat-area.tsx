'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useTaskMessages } from '@/hooks/useTaskMessages';
import { cn } from '@/lib/utils';

const KIND_STYLES: Record<string, string> = {
  user: 'bg-card',
  system: 'bg-muted text-muted-foreground italic',
  event: 'bg-secondary text-muted-foreground text-xs font-mono',
  stream: 'bg-background text-muted-foreground text-xs font-mono opacity-80',
  error: 'bg-destructive/10 text-destructive',
};

const SCROLL_BOTTOM_THRESHOLD_PX = 80;

function isAtBottom(el: HTMLElement, threshold = SCROLL_BOTTOM_THRESHOLD_PX) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
}

export function ChatArea({ taskId }: { taskId: number | null }) {
  const entries = useTaskMessages(taskId);
  const ref = useRef<HTMLDivElement>(null);
  const prevLenRef = useRef(0);
  const prevTaskIdRef = useRef<number | null>(null);
  const [pendingCount, setPendingCount] = useState(0);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const prevLen = prevLenRef.current;
    const prevTaskId = prevTaskIdRef.current;
    const taskChanged = taskId !== prevTaskId;
    const firstPaint = prevLen === 0 && entries.length > 0;
    const lastIsUser = entries.length > 0 && entries[entries.length - 1].kind === 'user';
    const userJustSent = lastIsUser && entries.length === prevLen + 1;
    const wasAtBottom = isAtBottom(el);
    const newDelta = Math.max(0, entries.length - prevLen);

    if (taskChanged || firstPaint || userJustSent || wasAtBottom) {
      el.scrollTo({ top: el.scrollHeight });
      setPendingCount(0);
    } else if (newDelta > 0) {
      setPendingCount((c) => c + newDelta);
    }

    prevLenRef.current = entries.length;
    prevTaskIdRef.current = taskId;
  }, [entries, taskId]);

  const onScroll = useCallback(() => {
    const el = ref.current;
    if (el && isAtBottom(el)) setPendingCount(0);
  }, []);

  const scrollToBottom = useCallback(() => {
    const el = ref.current;
    if (el) el.scrollTo({ top: el.scrollHeight });
    setPendingCount(0);
  }, []);

  if (taskId === null) {
    return <div className="flex flex-1 items-center justify-center text-muted-foreground">Select a task</div>;
  }
  if (!entries.length) {
    return <div className="flex flex-1 items-center justify-center text-muted-foreground">No messages yet</div>;
  }
  return (
    <div className="relative flex flex-1 flex-col min-h-0">
      <div ref={ref} onScroll={onScroll} className="flex-1 overflow-auto p-4">
        {entries.map((e, i) => (
          <div key={i} className={cn('mb-2 rounded p-3', KIND_STYLES[e.kind] || 'bg-card')}>
            {e.kind === 'user' && e.sender && (
              <div className="mb-1 text-xs uppercase tracking-wide text-muted-foreground">{e.sender}</div>
            )}
            <div className="whitespace-pre-wrap text-sm">{e.message}</div>
          </div>
        ))}
      </div>
      {pendingCount > 0 && (
        <button
          type="button"
          onClick={scrollToBottom}
          className="absolute bottom-4 right-4 z-10 rounded-full bg-primary px-3 py-1.5 text-xs text-primary-foreground shadow-lg hover:brightness-110"
        >
          ↓ {pendingCount} new {pendingCount === 1 ? 'message' : 'messages'}
        </button>
      )}
    </div>
  );
}
