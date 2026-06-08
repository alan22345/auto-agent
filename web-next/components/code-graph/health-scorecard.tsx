'use client';
import { Card } from '@/components/ui/card';
import type { RepoHealth } from '@/types/api';

interface Props {
  health: RepoHealth;
  poorFileCount: number;
}

// The composite score is a weighted blend of these five dimensions. Each row
// carries a one-line plain-language explanation so the score is self-documenting.
type SubScoreKey =
  | 'maintainability'
  | 'duplication'
  | 'dead_code'
  | 'cycles'
  | 'coupling';

const SUB_SCORES: { key: SubScoreKey; label: string; explain: string }[] = [
  {
    key: 'maintainability',
    label: 'Maintainability',
    explain: 'Complexity and cross-file coupling of the typical file.',
  },
  {
    key: 'duplication',
    label: 'Duplication',
    explain: 'Share of code that is copy-pasted across the repo.',
  },
  {
    key: 'dead_code',
    label: 'Dead code',
    explain:
      'Symbols/files with no caller, importer, or subclass (excludes test-only).',
  },
  {
    key: 'cycles',
    label: 'Cycles',
    explain: 'Code tangled in import/call dependency cycles.',
  },
  {
    key: 'coupling',
    label: 'Coupling',
    explain: 'How much code reaches across module-area boundaries.',
  },
];

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

      <div data-testid="health-subscores" className="space-y-2">
        {SUB_SCORES.map((s) => {
          const v = health[s.key];
          if (typeof v !== 'number') return null;
          return (
            <SubScoreBar
              key={s.key}
              label={s.label}
              value={v}
              explain={s.explain}
            />
          );
        })}
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

function SubScoreBar({
  label,
  value,
  explain,
}: {
  label: string;
  value: number;
  explain: string;
}) {
  const v = Math.round(value);
  const pct = Math.max(0, Math.min(100, v));
  const color = v >= 70 ? 'bg-emerald-500' : v >= 40 ? 'bg-amber-500' : 'bg-red-500';
  return (
    <div data-testid={`subscore-${label}`}>
      <div className="flex items-baseline justify-between text-xs">
        <span className="font-medium">{label}</span>
        <span className="tabular-nums text-muted-foreground">{v}</span>
      </div>
      <div className="mt-0.5 h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div
          className={`h-full rounded-full ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <p className="mt-0.5 text-[11px] leading-tight text-muted-foreground">
        {explain}
      </p>
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
