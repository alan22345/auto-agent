'use client';
import { CollapsibleSection } from './collapsible-section';
import type { DependencyCycle } from '@/types/api';

export function CyclesSection({ cycles }: { cycles: DependencyCycle[] }) {
  return (
    <CollapsibleSection title="Cycles" count={cycles.length} testId="cycles-section">
      {cycles.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No dependency cycles detected.
        </p>
      ) : (
        <ul className="space-y-2">
          {cycles.map((c) => (
            <li key={c.id} data-testid="cycle-row" className="text-xs">
              <span className="mr-2 rounded bg-muted px-1.5 py-0.5 font-semibold uppercase">
                {c.kind}
              </span>
              <span className="break-all font-mono">
                {c.members.join(' → ')} → {c.members[0]}
              </span>
            </li>
          ))}
        </ul>
      )}
    </CollapsibleSection>
  );
}
