'use client';
import { useState } from 'react';
import { wsClient } from '@/lib/ws';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

export function ApprovalBar({ taskId }: { taskId: number }) {
  const [feedback, setFeedback] = useState('');
  return (
    <div className="flex items-center gap-2 border-t bg-card p-2">
      <Input placeholder="Feedback (if rejecting)…" value={feedback} onChange={(e) => setFeedback(e.target.value)} />
      <Button onClick={() => wsClient.send({ type: 'approve', task_id: taskId })}>Approve</Button>
      <Button variant="destructive" onClick={() => { wsClient.send({ type: 'reject', task_id: taskId, feedback }); setFeedback(''); }}>Reject</Button>
    </div>
  );
}
