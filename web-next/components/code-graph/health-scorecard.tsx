'use client';
import { Card } from '@/components/ui/card';
import type { RepoHealth } from '@/types/api';

interface Props {
  health: RepoHealth;
  poorFileCount: number;
}

export function HealthScorecard({ health, poorFileCount }: Props) {
  const score = Math.round(health.score);
  const pct = Math.max(0, Math.min(100, score));
  return (
    <div data-testid="health-scorecard" className="space-y-3">
      <div>
        <div className="flex items-baseline justify-between">
          <span className="text-sm font-semibold">Repo health</span>
          <span
            data-testid="health-score"
            className="text-2xl font-bold tabular-nums"
          >
            {score}
            <span className="text-sm font-normal text-muted-foreground">
              /100
            </span>
          </span>
        </div>
        <div className="mt-1 h-2 w-full overflow-hidden rounded-full bg-muted">
          <div
            className="h-full rounded-full bg-primary"
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        <CountCard label="Cycles" value={health.cycle_count} />
        <CountCard label="Clones" value={health.clone_count} />
        <CountCard label="Dead code" value={health.dead_count} />
        <CountCard label="Hotspots" value={health.hotspot_count} />
        <CountCard label="Poor files" value={poorFileCount} />
      </div>
    </div>
  );
}

function CountCard({ label, value }: { label: string; value: number }) {
  return (
    <Card className="p-3 text-center">
      <p
        data-testid={`count-${label}`}
        className="text-xl font-bold tabular-nums"
      >
        {value}
      </p>
      <p className="text-xs text-muted-foreground">{label}</p>
    </Card>
  );
}
