'use client';
import { useState } from 'react';
import { markDone } from '@/lib/tasks';
import { Button } from '@/components/ui/button';

export function DoneBar({ taskId }: { taskId: number }) {
  const [busy, setBusy] = useState(false);
  async function go() { setBusy(true); try { await markDone(taskId); } finally { setBusy(false); } }
  return (
    <div className="flex items-center gap-2 border-t bg-card p-2">
      <Button onClick={go} disabled={busy}>{busy ? 'Marking…' : 'Mark Done'}</Button>
    </div>
  );
}
