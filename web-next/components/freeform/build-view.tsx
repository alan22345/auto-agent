'use client';
// Mirrors renderFreeformBuildNew() in web/static/index.html ~line 1247
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { wsClient } from '@/lib/ws';

export function BuildView() {
  const [description, setDescription] = useState('');
  const [org, setOrg] = useState('');
  const [loop, setLoop] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!description.trim()) {
      alert('Describe what you want to build');
      return;
    }
    setSubmitting(true);
    wsClient.send({ type: 'create_repo', description: description.trim(), org: org.trim(), loop });
    // TODO: listen for repo_created / error to reset submitting state (see web/static/index.html ~line 1553 onNextWsMessage usage)
    setDescription('');
    setOrg('');
    // Reset after a reasonable timeout since we don't have onNextWsMessage here yet
    setTimeout(() => setSubmitting(false), 200_000);
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
              Claude will pick a short repo name from this. Be specific about features you want in
              the first version.
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

        <Button type="submit" disabled={submitting}>
          {submitting ? 'Creating… this may take up to 3 minutes' : 'Create & Scaffold'}
        </Button>
      </form>
    </div>
  );
}
