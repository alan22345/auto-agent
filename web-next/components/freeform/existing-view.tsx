'use client';
// Mirrors renderFreeformAddExisting() in web/static/index.html ~line 1291
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { wsClient } from '@/lib/ws';
import { CRON_PRESETS } from '@/lib/cron-presets';

// Default cron for existing repos (less aggressive than greenfield)
const DEFAULT_CRON = '0 9 * * 1'; // Weekly (Mon 9am)

export function ExistingView() {
  const [repo, setRepo] = useState('');
  const [branch, setBranch] = useState('dev');
  const [cron, setCron] = useState(DEFAULT_CRON);
  const [auto, setAuto] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!repo.trim()) {
      alert('Enter a repo name');
      return;
    }
    setSubmitting(true);
    wsClient.send({
      type: 'toggle_freeform',
      repo_name: repo.trim(),
      enabled: true,
      dev_branch: branch.trim() || 'dev',
      analysis_cron: cron,
      auto_approve_suggestions: auto,
    });
    // TODO: listen for freeform_config_list to confirm success (see web/static/index.html ~line 1570 submitAddExisting)
    setTimeout(() => setSubmitting(false), 5000);
  }

  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-semibold mb-1">Add freeform to an existing repo</h1>
      <p className="text-sm text-muted-foreground mb-6">
        Enable freeform mode on a repo that&apos;s already registered with auto-agent. The PO agent
        will analyze it on a schedule and generate improvement suggestions.
      </p>

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Repo section */}
        <section className="space-y-4">
          <h2 className="text-base font-medium">Repo</h2>

          <div className="space-y-1.5">
            <Label htmlFor="ae-repo">Repo name</Label>
            <Input
              id="ae-repo"
              placeholder="e.g. cardamon or org/cardamon"
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              Must already be registered. The repo sync runs at startup; check the Tasks tab
              dropdown for the exact name.
            </p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="ae-branch">Dev branch</Label>
            <Input
              id="ae-branch"
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              PRs from freeform tasks will merge into this branch. Use{' '}
              <code className="text-xs bg-muted px-1 rounded">main</code> for greenfield repos.
            </p>
          </div>
        </section>

        {/* Analysis schedule section */}
        <section className="space-y-3">
          <h2 className="text-base font-medium">Analysis schedule</h2>
          <div className="space-y-1.5">
            <Label htmlFor="ae-cron">How often should the PO analyze the repo?</Label>
            <Select value={cron} onValueChange={setCron}>
              <SelectTrigger id="ae-cron">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CRON_PRESETS.map((p) => (
                  <SelectItem key={p.value} value={p.value}>
                    {p.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </section>

        {/* Options section */}
        <section className="space-y-3">
          <h2 className="text-base font-medium">Options</h2>
          <div className="flex items-start justify-between gap-4 rounded-md border border-border p-3">
            <div>
              <p className="text-sm font-medium">Auto-approve suggestions</p>
              <p className="text-xs text-muted-foreground mt-0.5">
                When the PO produces suggestions, automatically convert them into freeform tasks
                (no human click required). Capped at 5 active tasks per repo.
              </p>
            </div>
            <Switch checked={auto} onCheckedChange={setAuto} id="ae-auto" />
          </div>
        </section>

        <Button type="submit" disabled={submitting}>
          {submitting ? 'Enabling…' : 'Enable Freeform'}
        </Button>
      </form>
    </div>
  );
}
