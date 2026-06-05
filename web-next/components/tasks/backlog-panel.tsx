'use client';

import type { TaskData } from '@/types/api';

type BacklogItem = {
  id?: string;
  title?: string;
  description?: string;
  status?: string;
};

type Props = {
  backlog: TaskData['trio_backlog'];
};

const STATUS_COLORS: Record<string, string> = {
  done: 'bg-emerald-500/15 text-emerald-700 border-emerald-500/30',
  pending: 'bg-amber-500/15 text-amber-700 border-amber-500/30',
  blocked: 'bg-red-500/15 text-red-700 border-red-500/30',
};

const OVERSIZED_CONNECTIVES = [' + ', ' and ', ' with ', ' plus '];
const MAX_CONNECTIVES = 1;

// Mirror of agent/lifecycle/trio/gap_fix.py::_validate_item_size — the
// UI flags the same rows the orchestrator logs as oversized.
function isOversized(title: string | undefined): boolean {
  if (!title) return false;
  const lower = title.toLowerCase();
  let count = 0;
  for (const c of OVERSIZED_CONNECTIVES) {
    count += lower.split(c).length - 1;
  }
  return count > MAX_CONNECTIVES;
}

// Items added via the gap-fix architect path get IDs prefixed with "G"
// (see agent/lifecycle/trio/gap_fix.py — _assign_missing_ids defaults
// to ``G{N}`` for gap-fix dispatch_new). Original-backlog items use
// "T{N}". Surface the distinction so operators can see at a glance
// which work was promised in the design vs added later to close gaps.
function isGapFixItem(id: string | undefined): boolean {
  return !!id && /^G\d+$/i.test(id);
}

export function BacklogPanel({ backlog }: Props) {
  if (!backlog || backlog.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">No backlog items yet.</p>
    );
  }

  const items = backlog as BacklogItem[];

  return (
    <ul className="space-y-1">
      {items.map((item, idx) => {
        const status = (item.status ?? 'pending').toLowerCase();
        const colorClass =
          STATUS_COLORS[status] ?? 'bg-muted text-muted-foreground border-muted';
        const oversized = isOversized(item.title);
        return (
          <li
            key={`${item.id ?? '_'}_${idx}`}
            className="flex items-start gap-2 rounded border border-border/50 px-2 py-1.5 text-xs"
          >
            <span
              className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px] ${colorClass}`}
              title={`status: ${status}`}
            >
              {item.id ?? '?'}
            </span>
            <span className={`shrink-0 text-[10px] uppercase tracking-wide text-muted-foreground`}>
              {status}
            </span>
            {isGapFixItem(item.id) && (
              <span
                className="shrink-0 rounded border border-purple-500/40 bg-purple-500/10 px-1.5 py-0.5 text-[10px] font-medium text-purple-700"
                title="Added by the gap-fix architect to close a final-review gap"
              >
                gap-fix
              </span>
            )}
            <span className="grow break-words">
              {item.title ?? '(no title)'}
              {oversized && (
                <span
                  className="ml-2 rounded border border-amber-500/40 bg-amber-500/10 px-1 text-[10px] text-amber-700"
                  title="Title stitches multiple subsystems — architect was asked to split items like this"
                >
                  ⚠ oversized
                </span>
              )}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
