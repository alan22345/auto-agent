'use client';
import { useState } from 'react';
import { createTask } from '@/lib/tasks';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';

export function NewTaskForm() {
  const [title, setTitle] = useState('');
  const [desc, setDesc] = useState('');
  const [repo, setRepo] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!title.trim() || busy) return;
    setBusy(true);
    try {
      await createTask({ title: title.trim(), description: desc.trim() || undefined, repo: repo.trim() || undefined });
      setTitle(''); setDesc(''); setRepo('');
    } finally { setBusy(false); }
  }

  return (
    <div className="space-y-2 border-t p-3">
      <Input placeholder="Task title…" value={title} onChange={(e) => setTitle(e.target.value)} />
      <Textarea rows={2} placeholder="Description (optional)" value={desc} onChange={(e) => setDesc(e.target.value)} />
      <Input placeholder="Repo name (optional)" value={repo} onChange={(e) => setRepo(e.target.value)} />
      <Button onClick={submit} disabled={busy} className="w-full">
        {busy ? 'Creating…' : 'Create Task'}
      </Button>
    </div>
  );
}
