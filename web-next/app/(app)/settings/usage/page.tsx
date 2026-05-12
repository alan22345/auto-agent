'use client';

import { useUsageSummary } from '@/hooks/useUsage';

function Bar({ label, used, cap }: { label: string; used: number; cap: number }) {
  const pct = cap > 0 ? Math.min(100, Math.round((used / cap) * 100)) : 0;
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-sm">
        <span>{label}</span>
        <span className="tabular-nums text-muted-foreground">
          {used.toLocaleString()} / {cap.toLocaleString()}
        </span>
      </div>
      <div className="h-2 w-full rounded bg-muted">
        <div
          className="h-2 rounded bg-primary"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export default function UsagePage() {
  const { data, isLoading, error } = useUsageSummary();

  if (isLoading) return <div className="p-6">Loading usage…</div>;
  if (error || !data)
    return <div className="p-6 text-destructive">Failed to load usage.</div>;

  const { plan } = data;
  return (
    <div className="p-6 max-w-3xl space-y-8">
      <header>
        <h1 className="text-2xl font-semibold">Usage</h1>
        <p className="text-sm text-muted-foreground mt-2">
          Your current plan and today&apos;s consumption (UTC day).
        </p>
      </header>

      <section className="rounded-lg border p-6 space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-sm text-muted-foreground">Plan</div>
            <div className="text-xl font-medium capitalize">{plan.name}</div>
          </div>
          <button
            disabled
            className="rounded border px-3 py-1.5 text-sm opacity-60"
            title="Billing arrives in Phase 5"
          >
            Upgrade
          </button>
        </div>
        <Bar
          label="Active tasks"
          used={data.active_tasks}
          cap={plan.max_concurrent_tasks}
        />
        <Bar
          label="Tasks today"
          used={data.tasks_today}
          cap={plan.max_tasks_per_day}
        />
        <Bar
          label="Input tokens today"
          used={data.input_tokens_today}
          cap={plan.max_input_tokens_per_day}
        />
        <Bar
          label="Output tokens today"
          used={data.output_tokens_today}
          cap={plan.max_output_tokens_per_day}
        />
      </section>
    </div>
  );
}
