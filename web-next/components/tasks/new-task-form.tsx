'use client';
import { useState } from 'react';
import { createTask } from '@/lib/tasks';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';

export function NewTaskForm({ onCreated }: { onCreated?: () => void } = {}) {
  const [title, setTitle] = useState('');
  const [desc, setDesc] = useState('');
  const [repo, setRepo] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    if (!title.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await createTask({
        title: title.trim(),
        description: desc.trim() || undefined,
        repo: repo.trim() || undefined,
      });
      setTitle('');
      setDesc('');
      setRepo('');
      onCreated?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to create task');
    } finally {
      setBusy(false);
    }
  }

  function onTitleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter') {
      e.preventDefault();
      submit();
    }
  }

  function onDescKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      submit();
    }
  }

  return (
    <div className="flex flex-1 items-center justify-center overflow-auto p-8">
      <div className="w-full max-w-2xl space-y-6">
        <div className="space-y-1">
          <h2 className="text-2xl font-semibold tracking-tight">Create a new task</h2>
          <p className="text-sm text-muted-foreground">
            Give the agent a clear goal. Add context in the description — the more specific, the better.
          </p>
        </div>

        <div className="space-y-2">
          <Label htmlFor="task-title">Title</Label>
          <Input
            id="task-title"
            placeholder="What should the agent do?"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={onTitleKeyDown}
            className="h-11 text-base"
            autoFocus
          />
        </div>

        <div className="space-y-2">
          <Label htmlFor="task-desc">Description</Label>
          <Textarea
            id="task-desc"
            rows={8}
            placeholder="Acceptance criteria, links, constraints… (⌘/Ctrl+Enter to submit)"
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            onKeyDown={onDescKeyDown}
            className="min-h-[180px] resize-y text-sm"
          />
        </div>

        <div className="space-y-2">
          <Label htmlFor="task-repo">Repo</Label>
          <Input
            id="task-repo"
            placeholder="Optional — e.g. auto-agent"
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
          />
        </div>

        <div className="flex items-center justify-end gap-3 pt-2">
          {error && (
            <span className="text-xs text-destructive">{error}</span>
          )}
          <Button onClick={submit} disabled={busy || !title.trim()} size="lg">
            {busy ? 'Creating…' : 'Create Task'}
          </Button>
        </div>
      </div>
    </div>
  );
}
