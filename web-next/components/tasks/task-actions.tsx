'use client';
import { useState } from 'react';
import { cancelTask, deleteTask, setPriority } from '@/lib/tasks';
import type { TaskData } from '@/types/api';
import { Button } from '@/components/ui/button';

export function TaskActions({ task }: { task: TaskData }) {
  const [busy, setBusy] = useState(false);
  const showPriority = task.status === 'queued';

  async function doCancel() { setBusy(true); try { await cancelTask(task.id); } finally { setBusy(false); } }
  async function doDelete() {
    if (!confirm('Delete this task?')) return;
    setBusy(true); try { await deleteTask(task.id); } finally { setBusy(false); }
  }
  async function doPriority(p: number) { setBusy(true); try { await setPriority(task.id, p); } finally { setBusy(false); } }

  return (
    <div className="flex flex-wrap items-center gap-2 border-t p-2 text-xs">
      <Button size="sm" variant="secondary" disabled={busy} onClick={doCancel}>Cancel</Button>
      <Button size="sm" variant="destructive" disabled={busy} onClick={doDelete}>Delete</Button>
      {showPriority && (
        <span className="ml-auto flex items-center gap-1 text-muted-foreground">
          <span>Priority:</span>
          <Button size="sm" variant={task.priority === 50 ? 'default' : 'secondary'} onClick={() => doPriority(50)}>High</Button>
          <Button size="sm" variant={task.priority === 100 ? 'default' : 'secondary'} onClick={() => doPriority(100)}>Normal</Button>
          <Button size="sm" variant={task.priority === 200 ? 'default' : 'secondary'} onClick={() => doPriority(200)}>Low</Button>
        </span>
      )}
    </div>
  );
}
