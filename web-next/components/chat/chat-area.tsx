'use client';
import { useEffect, useRef } from 'react';
import { useTaskMessages } from '@/hooks/useTaskMessages';

export function ChatArea({ taskId }: { taskId: number | null }) {
  const messages = useTaskMessages(taskId);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { ref.current?.scrollTo({ top: ref.current.scrollHeight }); }, [messages]);

  if (taskId === null) {
    return <div className="flex flex-1 items-center justify-center text-muted-foreground">Select a task</div>;
  }
  if (!messages.length) {
    return <div className="flex flex-1 items-center justify-center text-muted-foreground">No messages yet</div>;
  }
  return (
    <div ref={ref} className="flex-1 overflow-auto p-4">
      {messages.map((m, i) => (
        <div key={i} className="mb-3 rounded bg-card p-3">
          <div className="mb-1 text-xs uppercase tracking-wide text-muted-foreground">{m.sender}</div>
          <div className="whitespace-pre-wrap text-sm">{m.content}</div>
        </div>
      ))}
    </div>
  );
}
