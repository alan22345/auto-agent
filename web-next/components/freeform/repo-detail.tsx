'use client';
// Mirrors renderFreeformRepoDetail() in web/static/index.html ~line 1346
import { useEffect, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { wsClient } from '@/lib/ws';
import { useWS } from '@/hooks/useWS';
import { CRON_PRESETS } from '@/lib/cron-presets';
import { useRepos, useUpdateProductBrief } from '@/hooks/useRepos';
import type { FreeformConfig } from '@/types/ws';

interface Props {
  config: FreeformConfig | null;
}

type SaveStatus =
  | { kind: 'idle' }
  | { kind: 'saving' }
  | { kind: 'saved' }
  | { kind: 'error'; message: string };

export function RepoDetail({ config }: Props) {
  const [enabled, setEnabled] = useState(false);
  const [auto, setAuto] = useState(false);
  const [autoStart, setAutoStart] = useState(false);
  const [branch, setBranch] = useState('dev');
  const [cron, setCron] = useState(CRON_PRESETS[0].value);
  const [archMode, setArchMode] = useState(false);
  const [archCron, setArchCron] = useState(CRON_PRESETS[0].value);
  const [poGoal, setPoGoal] = useState('');
  const [productBrief, setProductBrief] = useState('');
  const [status, setStatus] = useState<SaveStatus>({ kind: 'idle' });
  const savingRef = useRef(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Repo metadata (id + product_brief) comes from the REST /api/repos
  // endpoint — the WebSocket FreeformConfig payload doesn't include them.
  const reposQuery = useRepos();
  const repo = useMemo(
    () =>
      reposQuery.data?.find((r) => r.name === config?.repo_name) ?? null,
    [reposQuery.data, config?.repo_name],
  );
  const updateBriefMutation = useUpdateProductBrief();

  // Only re-sync from server when the user switches to a different repo —
  // not on every WS push. Otherwise an in-flight broadcast can clobber the
  // user's local edits (e.g. toggling enabled off, then a stale config push
  // resetting it back to on).
  useEffect(() => {
    if (!config) return;
    setEnabled(config.enabled);
    setAuto(config.auto_approve_suggestions);
    setAutoStart(config.auto_start_tasks ?? false);
    setBranch(config.dev_branch || 'dev');
    setCron(config.analysis_cron || CRON_PRESETS[0].value);
    setArchMode(config.architecture_mode ?? false);
    setArchCron(config.architecture_cron || CRON_PRESETS[0].value);
    setPoGoal(config.po_goal ?? '');
    setStatus({ kind: 'idle' });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config?.repo_name]);

  // product_brief lives on Repo (REST), not FreeformConfig (WS). Re-sync
  // when the user switches repos OR when the repos query first resolves.
  useEffect(() => {
    if (repo) setProductBrief(repo.product_brief ?? '');
  }, [repo?.id]);

  // Confirm save by waiting for the next freeform_config_list broadcast.
  useWS('freeform_config_list', (e) => {
    if (!savingRef.current) return;
    const next = e.configs.find((c) => c.repo_name === config?.repo_name);
    if (!next) return;
    savingRef.current = false;
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    setStatus({ kind: 'saved' });
    // Mirror the saved values back into local state so we stay in sync.
    setEnabled(next.enabled);
    setAuto(next.auto_approve_suggestions);
    setAutoStart(next.auto_start_tasks ?? false);
    setBranch(next.dev_branch || 'dev');
    setCron(next.analysis_cron || CRON_PRESETS[0].value);
    setArchMode(next.architecture_mode ?? false);
    setArchCron(next.architecture_cron || CRON_PRESETS[0].value);
    setPoGoal(next.po_goal ?? '');
  });

  useWS('error', (e) => {
    if (!savingRef.current) return;
    savingRef.current = false;
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    setStatus({ kind: 'error', message: e.message || 'Save failed' });
  });

  useEffect(() => () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
  }, []);

  if (!config) {
    return <p className="text-muted-foreground">Repo not found.</p>;
  }

  const isPreset = CRON_PRESETS.some((p) => p.value === cron);
  const isArchPreset = CRON_PRESETS.some((p) => p.value === archCron);
  const saving = status.kind === 'saving';

  function handleSave(e: React.FormEvent) {
    e.preventDefault();
    if (saving) return;

    const sent = wsClient.send({
      type: 'toggle_freeform',
      repo_name: config!.repo_name,
      enabled,
      dev_branch: branch.trim() || 'dev',
      analysis_cron: cron,
      auto_approve_suggestions: auto,
      auto_start_tasks: autoStart,
      po_goal: poGoal.trim() || null,
      architecture_mode: archMode,
      architecture_cron: archCron,
    });
    if (!sent) {
      setStatus({
        kind: 'error',
        message: 'Connection lost — reconnecting. Try again in a moment.',
      });
      return;
    }

    savingRef.current = true;
    setStatus({ kind: 'saving' });
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => {
      if (!savingRef.current) return;
      savingRef.current = false;
      setStatus({ kind: 'error', message: 'Save timed out — check your connection.' });
    }, 8000);
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

        {/* Improvement mode section — ADR-015 §14 renamed from
            "Architecture mode" so the label matches the improvement
            agent's role (codebase-deepening). The trio's task-decomposer
            architect keeps the "Architect" label elsewhere. */}
        <section className="space-y-3">
          <h2 className="text-base font-medium">Improvement mode</h2>

          <div className="flex items-start justify-between gap-4 rounded-md border border-border p-3">
            <div>
              <p className="text-sm font-medium">Continuous architectural improvements</p>
              <p className="text-xs text-muted-foreground mt-0.5">
                Periodically run the improvement agent on a readonly clone. Produces deepening
                Suggestions (no new features). Combine with auto-approve + auto-start above for
                end-to-end autonomy.
              </p>
            </div>
            <Switch checked={archMode} onCheckedChange={setArchMode} id="rd-archmode" />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="rd-archcron">Improvement schedule</Label>
            <Select
              value={isArchPreset ? archCron : '__custom__'}
              onValueChange={(v) => {
                if (v !== '__custom__') setArchCron(v);
              }}
            >
              <SelectTrigger id="rd-archcron">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {!isArchPreset && (
                  <SelectItem value="__custom__">Custom: {archCron}</SelectItem>
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
            <Label htmlFor="rd-pogoal">PO goal</Label>
            <Textarea
              id="rd-pogoal"
              value={poGoal}
              onChange={(e) => setPoGoal(e.target.value)}
              placeholder="e.g. Convert more first-time visitors into signups by reducing onboarding friction."
              rows={3}
            />
            <p className="text-xs text-muted-foreground">
              Free-text objective the PO analyzer evaluates suggestions against. Leave blank for an open-ended scan.
            </p>
          </div>

          {/*
            Product brief — repo-scoped markdown context injected into every
            PO prompt (today: architect clarification answers). Saved via
            REST PATCH /api/repos/{id}/product-brief, NOT the WS form below,
            because the brief is meaningful for every repo regardless of
            freeform mode. Rendered inside the freeform settings panel
            today because it is the only repo-settings surface in the UI.
          */}
          <div className="space-y-1.5">
            <Label htmlFor="rd-product-brief">Product brief</Label>
            <Textarea
              id="rd-product-brief"
              value={productBrief}
              onChange={(e) => setProductBrief(e.target.value)}
              placeholder={
                '# Mission\nWhat does this repo build, for whom, and what are the non-goals?'
              }
              rows={8}
              disabled={!repo}
            />
            <p className="text-xs text-muted-foreground">
              Markdown brief describing this repo&apos;s mission, requirements, and non-goals.
              Used by the PO agent when answering architect clarification questions.
            </p>
            <div className="flex items-center gap-3 pt-1">
              <Button
                type="button"
                variant="secondary"
                size="sm"
                disabled={!repo || updateBriefMutation.isPending}
                onClick={() => {
                  if (!repo) return;
                  updateBriefMutation.mutate({
                    repoId: repo.id,
                    brief: productBrief,
                  });
                }}
              >
                {updateBriefMutation.isPending ? 'Saving…' : 'Save product brief'}
              </Button>
              {updateBriefMutation.isSuccess && (
                <span className="text-xs text-success">Saved.</span>
              )}
              {updateBriefMutation.isError && (
                <span className="text-xs text-destructive">
                  {updateBriefMutation.error instanceof Error
                    ? updateBriefMutation.error.message
                    : 'Save failed.'}
                </span>
              )}
            </div>
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

        <div className="flex items-center gap-3">
          <Button type="submit" disabled={saving}>
            {saving ? 'Saving…' : 'Save changes'}
          </Button>
          {status.kind === 'saved' && (
            <span className="text-xs text-success">Saved.</span>
          )}
          {status.kind === 'error' && (
            <span className="text-xs text-destructive">{status.message}</span>
          )}
        </div>
      </form>
    </div>
  );
}
