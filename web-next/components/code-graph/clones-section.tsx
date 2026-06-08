'use client';
import { CollapsibleSection } from './collapsible-section';
import type { CloneGroup } from '@/types/api';

export function ClonesSection({ clones }: { clones: CloneGroup[] }) {
  return (
    <CollapsibleSection title="Clones" count={clones.length} testId="clones-section">
      {clones.length === 0 ? (
        <p className="text-xs text-muted-foreground">No code clones detected.</p>
      ) : (
        <ul className="space-y-2">
          {clones.map((g) => (
            <li key={g.id} data-testid="clone-row" className="text-xs">
              <p className="font-semibold">
                <span className="mr-2 rounded bg-muted px-1.5 py-0.5 uppercase">
                  {g.mode}
                </span>
                {g.instances.length} instances · {g.token_len} tokens
              </p>
              <ul className="ml-3 mt-1 space-y-0.5 font-mono text-muted-foreground">
                {g.instances.map((inst) => (
                  <li key={inst.node_id} className="break-all">
                    {inst.file}:{inst.line_start}-{inst.line_end}
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </CollapsibleSection>
  );
}
