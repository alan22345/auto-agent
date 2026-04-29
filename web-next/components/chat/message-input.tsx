'use client';
import { useState } from 'react';
import { wsClient } from '@/lib/ws';
import type { TaskData } from '@/types/api';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';

export function MessageInput({ task }: { task: TaskData }) {
  const [v, setV] = useState('');
  const isActive = task.status === 'coding' || task.status === 'planning';
  const placeholder = isActive
    ? 'Send guidance to the agent (pair-programming)…'
    : 'Describe a task or send a message…';

  function send() {
    if (!v.trim()) return;
    const type = isActive ? 'send_guidance' : 'send_message';
    wsClient.send({ type, task_id: task.id, message: v });
    setV('');
  }

  return (
    <div className="flex gap-2 border-t p-2">
      <Input
        placeholder={placeholder}
        value={v}
        onChange={(e) => setV(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') send(); }}
      />
      <Button onClick={send}>Send</Button>
    </div>
  );
}
