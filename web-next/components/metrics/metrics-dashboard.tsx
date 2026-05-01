'use client';
import { useState } from 'react';
import { useMetrics } from '@/hooks/useMetrics';
import { cn } from '@/lib/utils';
import type { MetricsResponse, PROutcomeMetrics } from '@/types/api';

const PERIODS = [7, 30, 90] as const;

export function MetricsDashboard() {
  const [days, setDays] = useState<number>(30);
  const { data, error, isLoading } = useMetrics(days);

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Metrics</h1>
        <div role="tablist" aria-label="Metrics period" className="flex gap-1 rounded-md border p-1">
          {PERIODS.map((d) => (
            <button
              key={d}
              role="tab"
              aria-selected={days === d}
              onClick={() => setDays(d)}
              className={cn(
                'rounded px-3 py-1 text-xs font-medium text-muted-foreground hover:text-foreground',
                days === d && 'bg-secondary text-foreground',
              )}
            >
              {d}d
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div className="rounded border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
          Failed to load metrics: {(error as Error).message}
        </div>
      )}

      {isLoading && !data && (
        <div className="text-sm text-muted-foreground">Loading…</div>
      )}

      {data && <MetricsContent data={data} />}
    </div>
  );
}

function MetricsContent({ data }: { data: MetricsResponse }) {
  const finished = (data.by_status?.done ?? 0) + (data.by_status?.failed ?? 0);
  const successRate = finished === 0 ? '—' : `${(data.success_rate_pct ?? 0).toFixed(1)}%`;
  const avgHours =
    data.avg_duration_hours == null ? '—' : `${Number(data.avg_duration_hours).toFixed(1)} h`;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <KpiCard label="Total tasks" value={data.total_tasks} sub={`last ${data.period_days}d`} />
        <KpiCard label="Active tasks" value={data.active_tasks} sub="in flight right now" />
        <KpiCard label="Success rate" value={successRate} sub="done vs. failed" />
        <KpiCard label="Avg duration" value={avgHours} sub="intake → done" />
      </div>

      <BreakdownCard title="By status" map={data.by_status || {}} />
      <BreakdownCard title="By source" map={data.by_source || {}} />
      <PrOutcomesCard pr={data.pr_outcomes || ({} as PROutcomeMetrics)} />
    </div>
  );
}

function KpiCard({
  label,
  value,
  sub,
}: {
  label: string;
  value: number | string;
  sub: string;
}) {
  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
      <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>
    </div>
  );
}

function BreakdownCard({ title, map }: { title: string; map: Record<string, number> }) {
  const entries = Object.entries(map).sort((a, b) => b[1] - a[1]);
  const max = Math.max(...entries.map(([, c]) => c), 1);
  const total = entries.reduce((a, [, c]) => a + c, 0);

  return (
    <div className="rounded-lg border bg-card p-4">
      <h3 className="mb-3 text-sm font-medium">{title}</h3>
      {entries.length === 0 ? (
        <div className="text-sm text-muted-foreground">No data</div>
      ) : (
        <div className="space-y-2">
          {entries.map(([key, count]) => {
            const widthPct = (count / max) * 100;
            const sharePct = total > 0 ? `${((count / total) * 100).toFixed(1)}%` : '—';
            return (
              <div key={key} className="grid grid-cols-[10rem_1fr_6rem] items-center gap-3 text-xs">
                <span className="truncate font-mono text-muted-foreground" title={key}>
                  {key}
                </span>
                <span className="h-2 rounded bg-secondary">
                  <span
                    className="block h-full rounded bg-primary"
                    style={{ width: `${widthPct}%` }}
                  />
                </span>
                <span className="text-right tabular-nums text-muted-foreground">
                  {count} · {sharePct}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function PrOutcomesCard({ pr }: { pr: PROutcomeMetrics }) {
  const total = pr.total ?? 0;
  return (
    <div className="rounded-lg border bg-card p-4">
      <h3 className="mb-3 text-sm font-medium">PR Outcomes</h3>
      {total === 0 ? (
        <div className="text-sm text-muted-foreground">No PR outcomes in this period yet.</div>
      ) : (
        <div className="grid grid-cols-2 gap-3">
          <KpiCard
            label="Approval rate"
            value={`${(pr.approval_rate_pct ?? 0).toFixed(1)}%`}
            sub={`${pr.approved ?? 0}/${total} approved`}
          />
          <KpiCard
            label="Avg review rounds"
            value={(pr.avg_review_rounds ?? 0).toFixed(1)}
            sub={`across ${total} PR${total === 1 ? '' : 's'}`}
          />
        </div>
      )}
    </div>
  );
}
