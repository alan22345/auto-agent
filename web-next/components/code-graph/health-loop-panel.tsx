'use client';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  useHealthFindings,
  useHealthLoop,
  useHealthLoopActions,
} from '@/hooks/useHealthLoop';
import type { HealthLoopState, HealthLoopStatus } from '@/lib/health-loop';

const STATE_VARIANT: Record<HealthLoopState, 'default' | 'secondary' | 'outline'> = {
  running: 'default',
  idle: 'secondary',
  paused: 'outline',
};

function StatusStrip({ status }: { status: HealthLoopStatus }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
      <span>
        merged <span className="font-semibold text-foreground">{status.merged_count}</span>
      </span>
      <span>
        parked <span className="font-semibold text-foreground">{status.parked_count}</span>
      </span>
      <span>
        suppressed <span className="font-semibold text-foreground">{status.suppressed_count}</span>
      </span>
      <span>
        remaining <span className="font-semibold text-foreground">{status.remaining_count}</span>
      </span>
      <span className="font-mono">{status.cleanup_branch}</span>
      {status.cleanup_pr_url && (
        <a
          href={status.cleanup_pr_url}
          target="_blank"
          rel="noreferrer"
          className="text-primary underline-offset-4 hover:underline"
        >
          cleanup PR ↗
        </a>
      )}
    </div>
  );
}

export function HealthLoopPanel({ repoId }: { repoId: number }) {
  const { data: status, isLoading } = useHealthLoop(repoId);
  const { data: findings } = useHealthFindings(repoId);
  const { start, stop, resume, suppress } = useHealthLoopActions(repoId);

  if (isLoading || !status) {
    return (
      <Card data-testid="health-loop-panel">
        <CardContent className="py-4 text-sm text-muted-foreground">Loading auto-heal…</CardContent>
      </Card>
    );
  }

  const active = status.enabled && status.state !== 'paused';
  const busy = start.isPending || stop.isPending || resume.isPending;

  return (
    <Card data-testid="health-loop-panel">
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 text-base">
            Auto-heal
            <Badge variant={STATE_VARIANT[status.state]} data-testid="health-loop-state">
              {status.state}
            </Badge>
          </CardTitle>
          <div className="flex items-center gap-2">
            {!status.enabled || status.state === 'paused' ? (
              <Button
                size="sm"
                disabled={busy}
                onClick={() => (status.enabled ? resume.mutate() : start.mutate())}
              >
                {status.enabled ? 'Resume' : 'Start'}
              </Button>
            ) : (
              <Button size="sm" variant="outline" disabled={busy} onClick={() => stop.mutate()}>
                Stop
              </Button>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-xs text-muted-foreground">
          Drains health findings onto <span className="font-mono">{status.cleanup_branch}</span>{' '}
          behind CI + smoke + differential gates. Never merges to your default branch — a human
          reviews the cleanup PR. While active, it holds an exclusive lease that pauses all other
          task dispatch.
        </p>

        <StatusStrip status={status} />

        {active && status.current_batch.length > 0 && (
          <div
            className="rounded-md border bg-card/40 p-2 text-xs"
            data-testid="health-loop-inflight"
          >
            <div className="mb-1 font-semibold">In flight ({status.current_batch.length})</div>
            <ul className="space-y-0.5 text-muted-foreground">
              {status.current_batch.map((item) => (
                <li key={item.hash} className="truncate">
                  {item.title}
                </li>
              ))}
            </ul>
          </div>
        )}

        {findings && findings.length > 0 && (
          <div className="space-y-1">
            <div className="text-xs font-semibold text-muted-foreground">
              Findings (worst first)
            </div>
            <ul className="divide-y rounded-md border">
              {findings.map((f) => (
                <li
                  key={f.finding_hash}
                  className="flex items-center justify-between gap-2 px-2 py-1.5 text-xs"
                >
                  <span className="flex min-w-0 items-center gap-2">
                    <Badge variant="outline" className="shrink-0">
                      {f.category}
                    </Badge>
                    <span className="truncate" title={f.title}>
                      {f.title}
                    </span>
                  </span>
                  {f.suppressed ? (
                    <Badge variant="secondary" className="shrink-0">
                      suppressed
                    </Badge>
                  ) : (
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-7 shrink-0 px-2"
                      disabled={suppress.isPending}
                      onClick={() => suppress.mutate(f.finding_hash)}
                    >
                      Suppress
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
