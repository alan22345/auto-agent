'use client';
import { useMemo, useState } from 'react';
import { createTask, type ModeOverride } from '@/lib/tasks';
import { useRepos } from '@/hooks/useRepos';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';

// ADR-015 §7 Phase 12 — bidirectional per-task override of the repo's
// default mode. The toggle label flips based on the repo's mode so the
// UI mirrors the resolver's bias: freeform repos surface "Force human
// review", human-in-loop repos surface "Run in freeform mode". Selecting
// nothing sends ``mode_override: null`` and the task inherits the repo.

export function NewTaskForm({ onCreated }: { onCreated?: () => void } = {}) {
  const [title, setTitle] = useState('');
  const [desc, setDesc] = useState('');
  const [repo, setRepo] = useState('');
  const [overrideMode, setOverrideMode] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: repos = [] } = useRepos();
  // The repo selector is a free-text input today (legacy); look up its
  // resolved repo by name so we can label the toggle correctly. When no
  // match is found we fall back to the conservative ``human_in_loop``
  // label.
  const resolvedRepo = useMemo(() => {
    const needle = repo.trim().toLowerCase();
    if (!needle) return null;
    return (
      repos.find((r) => r.name.toLowerCase() === needle) ??
      repos.find((r) => r.name.toLowerCase().endsWith(`/${needle}`)) ??
      null
    );
  }, [repos, repo]);
  const repoMode: 'freeform' | 'human_in_loop' = resolvedRepo?.mode ?? 'human_in_loop';
  // Override direction inverts the repo default — that's the bidirectional
  // contract from §7. If the user doesn't flip the switch we send null.
  const overrideTarget: ModeOverride =
    overrideMode ? (repoMode === 'freeform' ? 'human_in_loop' : 'freeform') : null;
  const overrideLabel =
    repoMode === 'freeform'
      ? 'Force human review for this task'
      : 'Run this task in freeform mode';
  const overrideHelp =
    repoMode === 'freeform'
      ? 'Repo default: freeform. Toggling on routes this task through the human-in-loop gates.'
      : 'Repo default: human-in-loop. Toggling on lets the standin agents approve every gate.';

  async function submit() {
    if (!title.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await createTask({
        title: title.trim(),
        description: desc.trim() || undefined,
        repo: repo.trim() || undefined,
        modeOverride: overrideTarget,
      });
      setTitle('');
      setDesc('');
      setRepo('');
      setOverrideMode(false);
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

        <div className="space-y-1 rounded border bg-muted/30 p-3">
          <div className="flex items-center justify-between gap-3">
            <div className="space-y-0.5">
              <Label htmlFor="task-mode-override" className="cursor-pointer text-sm">
                {overrideLabel}
              </Label>
              <p className="text-xs text-muted-foreground">{overrideHelp}</p>
            </div>
            <Switch
              id="task-mode-override"
              checked={overrideMode}
              onCheckedChange={setOverrideMode}
              aria-label={overrideLabel}
            />
          </div>
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
