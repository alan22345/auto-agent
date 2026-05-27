'use client';
import { useCallback, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { wsClient } from '@/lib/ws';
import { useWS } from '@/hooks/useWS';

export function BuildView() {
  const [description, setDescription] = useState('');
  const [name, setName] = useState('');
  const [org, setOrg] = useState('');
  const [loop, setLoop] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  useWS('repo_created', useCallback((e) => {
    setSubmitting(false);
    setError(null);
    setSuccess(`Created ${e.repo.name} — scaffold task #${e.task.id} queued.`);
    setDescription('');
    setName('');
    setOrg('');
  }, []));

  useWS('error', useCallback((e) => {
    if (!submitting) return;
    setSubmitting(false);
    setError(e.message);
  }, [submitting]));

  function handleSubmit(ev: React.FormEvent) {
    ev.preventDefault();
    if (!description.trim()) {
      setError('Describe what you want to build');
      return;
    }
    setError(null);
    setSuccess(null);
    setSubmitting(true);
    const sent = wsClient.send({
      type: 'create_repo',
      description: description.trim(),
      name: name.trim(),
      org: org.trim(),
      loop,
    });
    if (!sent) {
      setSubmitting(false);
      setError('Not connected to the server — try again in a moment.');
    }
  }

  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-semibold mb-1">Build something new</h1>
      <p className="text-sm text-muted-foreground mb-6">
        Describe a project and auto-agent will create a new GitHub repo, scaffold the initial
        codebase, and (if you enable the loop) keep improving it on its own.
      </p>

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Project description section */}
        <section className="space-y-4">
          <h2 className="text-base font-medium">Project description</h2>

          <div className="space-y-1.5">
            <Label htmlFor="cr-description">What do you want to build?</Label>
            <Textarea
              id="cr-description"
              rows={4}
              placeholder="e.g. A Next.js todo app with dark mode, drag-and-drop, and local storage persistence"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              Be specific about features you want in the first version.
            </p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="cr-name">Repo name (optional)</Label>
            <Input
              id="cr-name"
              placeholder="Leave empty and Claude will pick a slug from the description"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              Lowercase letters, digits, and hyphens. Anything else gets sanitised.
            </p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="cr-org">GitHub org (optional)</Label>
            <Input
              id="cr-org"
              placeholder="Leave empty to use your personal account"
              value={org}
              onChange={(e) => setOrg(e.target.value)}
            />
          </div>
        </section>

        {/* Options section */}
        <section className="space-y-3">
          <h2 className="text-base font-medium">Options</h2>
          <div className="flex items-start justify-between gap-4 rounded-md border border-border p-3">
            <div>
              <p className="text-sm font-medium">Continuous improvement loop</p>
              <p className="text-xs text-muted-foreground mt-0.5">
                After scaffolding, the PO agent generates suggestions every 30 min and
                auto-approves them. The repo will keep improving on its own until you turn this
                off.
              </p>
            </div>
            <Switch checked={loop} onCheckedChange={setLoop} id="cr-loop" />
          </div>
        </section>

        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
            {error}
          </div>
        )}
        {success && (
          <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm text-emerald-700 dark:text-emerald-300">
            {success}
          </div>
        )}

        <Button type="submit" disabled={submitting}>
          {submitting ? 'Creating… this may take up to 3 minutes' : 'Create & Scaffold'}
        </Button>
      </form>
    </div>
  );
}
