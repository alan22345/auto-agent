'use client';
import { useState } from 'react';
import { wsClient } from '@/lib/ws';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

export function ClarificationBar({ taskId }: { taskId: number }) {
  const [answer, setAnswer] = useState('');
  function submit() {
    if (!answer.trim()) return;
    wsClient.send({ type: 'send_message', task_id: taskId, message: answer });
    setAnswer('');
  }
  return (
    <div className="flex gap-2 border-t bg-card p-2">
      <Input placeholder="Type your answer…" value={answer} onChange={(e) => setAnswer(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') submit(); }} />
      <Button onClick={submit}>Answer</Button>
    </div>
  );
}
