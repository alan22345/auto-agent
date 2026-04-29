'use client';
// Mirrors renderFreeformRepoDetail() in web/static/index.html ~line 1346
import { useEffect, useState } from 'react';
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
import type { FreeformConfig } from '@/types/ws';

interface Props {
  config: FreeformConfig | null;
}

export function RepoDetail({ config }: Props) {
  const [enabled, setEnabled] = useState(false);
  const [auto, setAuto] = useState(false);
  const [autoStart, setAutoStart] = useState(false);
  const [branch, setBranch] = useState('dev');
  const [cron, setCron] = useState(CRON_PRESETS[0].value);
  const [saving, setSaving] = useState(false);

  // Sync form when config changes (e.g. on first load or repo switch)
  useEffect(() => {
    if (!config) return;
    setEnabled(config.enabled);
    setAuto(config.auto_approve_suggestions);
    setAutoStart(config.auto_start_tasks ?? false);
    setBranch(config.dev_branch || 'dev');
    setCron(config.analysis_cron || CRON_PRESETS[0].value);
  }, [config]);

  if (!config) {
    return <p className="text-muted-foreground">Repo not found.</p>;
  }

  // Determine if the current cron value is one of the presets (for the select)
  const isPreset = CRON_PRESETS.some((p) => p.value === cron);

  function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    wsClient.send({
      type: 'toggle_freeform',
      repo_name: config!.repo_name,
      enabled,
      dev_branch: branch.trim() || 'dev',
      analysis_cron: cron,
      auto_approve_suggestions: auto,
      auto_start_tasks: autoStart,
    });
    // TODO: listen for freeform_config_list / error to show toast feedback (see web/static/index.html ~line 1586 saveRepoConfig)
    setTimeout(() => setSaving(false), 5000);
  }

  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-semibold mb-1">{config.repo_name}</h1>
      <p className="text-sm text-muted-foreground mb-6">
        Freeform configuration and live activity for this repo.
      </p>

      <form onSubmit={handleSave} className="space-y-6">
        {/* Status section */}
        <section className="space-y-3">
          <h2 className="text-base font-medium">Status</h2>

          <div className="flex items-start justify-between gap-4 rounded-md border border-border p-3">
            <div>
              <p className="text-sm font-medium">Freeform mode enabled</p>
              <p className="text-xs text-muted-foreground mt-0.5">
                Master switch. When off, the PO loop stops and no new analysis runs.
              </p>
            </div>
            <Switch checked={enabled} onCheckedChange={setEnabled} id="rd-enabled" />
          </div>

          <div className="flex items-start justify-between gap-4 rounded-md border border-border p-3">
            <div>
              <p className="text-sm font-medium">Auto-approve suggestions</p>
              <p className="text-xs text-muted-foreground mt-0.5">
                Automatically convert PO suggestions into freeform tasks. Capped at 5 active tasks
                per repo.
              </p>
            </div>
            <Switch checked={auto} onCheckedChange={setAuto} id="rd-auto" />
          </div>

          <div className="flex items-start justify-between gap-4 rounded-md border border-border p-3">
            <div>
              <p className="text-sm font-medium">Auto-start tasks</p>
              <p className="text-xs text-muted-foreground mt-0.5">
                Immediately start work on approved freeform tasks, bypassing the concurrency queue.
              </p>
            </div>
            <Switch checked={autoStart} onCheckedChange={setAutoStart} id="rd-autostart" />
          </div>
        </section>

        {/* Configuration section */}
        <section className="space-y-4">
          <h2 className="text-base font-medium">Configuration</h2>

          <div className="space-y-1.5">
            <Label htmlFor="rd-branch">Dev branch</Label>
            <Input
              id="rd-branch"
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              Freeform PRs auto-merge into this branch when CI passes.
            </p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="rd-cron">Analysis schedule</Label>
            <Select
              value={isPreset ? cron : '__custom__'}
              onValueChange={(v) => {
                if (v !== '__custom__') setCron(v);
              }}
            >
              <SelectTrigger id="rd-cron">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {!isPreset && (
                  <SelectItem value="__custom__">Custom: {cron}</SelectItem>
                )}
                {CRON_PRESETS.map((p) => (
                  <SelectItem key={p.value} value={p.value}>
                    {p.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </section>

        {/* Last analysis info */}
        {config.last_analysis_at && (
          <p className="text-xs text-muted-foreground">
            Last analysis: {new Date(config.last_analysis_at).toLocaleString()}
          </p>
        )}

        {/* TODO: recent tasks list for this repo (see web/static/index.html ~line 1450 #rd-recent-tasks) */}
        {/* TODO: pending suggestions list + approve/reject actions (see web/static/index.html ~line 1443 #rd-suggestions) */}
        {/* TODO: "Run PO analysis now" button with trigger_analysis WS command (see web/static/index.html ~line 1423 triggerAnalysisFor) */}

        <Button type="submit" disabled={saving}>
          {saving ? 'Saving…' : 'Save changes'}
        </Button>
      </form>
    </div>
  );
}
