'use client';

import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useGapFixState } from '@/hooks/useTrioArtifacts';

const ACTION_TONE: Record<string, string> = {
  dispatch_new: 'bg-amber-500/15 text-amber-700 border-amber-500/30',
  escalate: 'bg-blue-500/15 text-blue-700 border-blue-500/30',
  blocked: 'bg-red-500/15 text-red-700 border-red-500/30',
};

const ACTION_LABEL: Record<string, string> = {
  dispatch_new: 'dispatched new items',
  escalate: 'escalated',
  blocked: 'blocked',
};

export function GapFixPanel({ taskId }: { taskId: number }) {
  const [open, setOpen] = useState(true);
  const { data, isLoading } = useGapFixState(taskId);

  if (isLoading || !data) return null;
  // Hide the panel entirely when the trio hasn't entered gap-fix yet.
  if (data.rounds_completed === 0 && data.gaps.length === 0) return null;

  const action = data.latest_action ?? '';
  const tone = ACTION_TONE[action] ?? 'bg-muted text-muted-foreground border-muted';
  const label = ACTION_LABEL[action] ?? action;

  return (
    <section className="space-y-2 rounded border border-border/60 p-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 text-left"
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />
        )}
        <span className="text-xs font-semibold">
          Gap-fix
          {data.max_rounds > 0 && (
            <span className="ml-1 font-normal text-muted-foreground">
              round {data.rounds_completed} of {data.max_rounds}
            </span>
          )}
        </span>
        {action && (
          <span
            className={`ml-2 rounded border px-1.5 py-0.5 text-[10px] font-medium ${tone}`}
          >
            {label}
            {action === 'dispatch_new' && data.latest_item_count > 0 && (
              <> · {data.latest_item_count} items</>
            )}
            {data.latest_oversized_count > 0 && (
              <> · {data.latest_oversized_count} ⚠ oversized</>
            )}
          </span>
        )}
      </button>

      {open && data.gaps.length > 0 && (
        <div className="space-y-1.5 pt-1">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
            Final reviewer found {data.gaps.length} gap{data.gaps.length === 1 ? '' : 's'}
          </div>
          <ul className="space-y-1">
            {data.gaps.map((g, idx) => (
              <li
                key={idx}
                className="rounded border border-border/40 bg-muted/30 px-2 py-1.5 text-xs"
              >
                <div className="whitespace-pre-wrap break-words">
                  {g.description}
                </div>
                {g.affected_routes && g.affected_routes.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1 text-[10px] text-muted-foreground">
                    {g.affected_routes.map((r) => (
                      <code key={r} className="rounded bg-background px-1 py-0.5">
                        {r}
                      </code>
                    ))}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {open && data.gaps.length === 0 && data.rounds_completed > 0 && (
        <div className="text-[11px] text-muted-foreground">
          No outstanding gaps. (Last round {action ? `→ ${label}` : 'completed'}.)
        </div>
      )}
    </section>
  );
}
