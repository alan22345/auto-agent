'use client';
import { useState } from 'react';
import { wsClient } from '@/lib/ws';
import { markDone } from '@/lib/tasks';
import type { TaskData } from '@/types/api';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

const DONE_STATUSES = new Set(['awaiting_review', 'queued', 'blocked', 'failed']);

export function MessageInput({ task }: { task: TaskData }) {
  const [v, setV] = useState('');
  const [busy, setBusy] = useState(false);

  const isFreeform = task.freeform_mode === true;
  const isPairing = task.status === 'coding' || task.status === 'planning';
  const isClarification = task.status === 'awaiting_clarification';
  const isApproval = task.status === 'awaiting_approval' && !isFreeform;
  const showDone = DONE_STATUSES.has(task.status) && !isFreeform;

  const placeholder = isApproval
    ? 'Optional feedback (required to reject)…'
    : isClarification
      ? 'Answer the agent — or ask a question back…'
      : isPairing
        ? 'Send guidance to the agent (pair-programming)…'
        : 'Describe a task or send a message…';

  const hint = isApproval
    ? 'Plan ready — approve, or reject with feedback'
    : isClarification
      ? 'Agent is waiting for your answer'
      : isPairing
        ? 'Agent is working — your message will be injected as guidance'
        : null;

  const attention = isApproval || isClarification;

  function sendMessage() {
    if (!v.trim()) return;
    const type = isPairing ? 'send_guidance' : 'send_message';
    wsClient.send({ type, task_id: task.id, message: v });
    setV('');
  }

  function approve() {
    wsClient.send({ type: 'approve', task_id: task.id });
    setV('');
  }

  function reject() {
    wsClient.send({ type: 'reject', task_id: task.id, feedback: v });
    setV('');
  }

  async function done() {
    setBusy(true);
    try { await markDone(task.id); } finally { setBusy(false); }
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key !== 'Enter') return;
    if (isApproval) reject();
    else sendMessage();
  }

  return (
    <div
      className={cn(
        'border-t p-2',
        attention && 'border-t-2 border-primary bg-primary/5',
      )}
    >
      {hint && (
        <div className={cn(
          'mb-1.5 px-1 text-xs',
          attention ? 'font-medium text-primary' : 'text-muted-foreground',
        )}>
          {attention ? '↑ ' : ''}{hint}
        </div>
      )}
      <div className="flex gap-2">
        <Input
          autoFocus={attention}
          placeholder={placeholder}
          value={v}
          onChange={(e) => setV(e.target.value)}
          onKeyDown={onKeyDown}
        />
        {isApproval ? (
          <>
            <Button onClick={approve}>Approve</Button>
            <Button variant="destructive" onClick={reject} disabled={!v.trim()}>
              Reject
            </Button>
          </>
        ) : (
          <Button
            onClick={sendMessage}
            variant={isClarification ? 'default' : 'secondary'}
            disabled={!v.trim()}
          >
            {isClarification ? 'Reply' : 'Send'}
          </Button>
        )}
        {showDone && (
          <Button variant="outline" onClick={done} disabled={busy}>
            {busy ? 'Marking…' : 'Mark Done'}
          </Button>
        )}
      </div>
    </div>
  );
}
