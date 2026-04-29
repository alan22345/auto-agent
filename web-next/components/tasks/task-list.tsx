'use client';
import { TaskData } from '@/types/api';
import { cn } from '@/lib/utils';

const STATUS_COLORS: Record<string, string> = {
  intake: 'text-muted-foreground',
  classifying: 'text-muted-foreground',
  queued: 'text-muted-foreground',
  planning: 'text-accent',
  coding: 'text-accent',
  awaiting_approval: 'text-primary',
  awaiting_clarification: 'text-primary',
  awaiting_review: 'text-primary',
  pr_created: 'text-success',
  awaiting_ci: 'text-success',
  done: 'text-success',
  blocked: 'text-destructive',
  failed: 'text-destructive',
};

export function TaskList({
  tasks, selectedId, onSelect,
}: { tasks: TaskData[]; selectedId: number | null; onSelect: (id: number) => void }) {
  if (!tasks.length) {
    return <div className="p-4 text-sm text-muted-foreground">No tasks yet</div>;
  }
  return (
    <ul className="flex flex-col">
      {tasks.map((t) => (
        <li key={t.id}>
          <button
            onClick={() => onSelect(t.id)}
            className={cn(
              'w-full px-3 py-2 text-left text-sm hover:bg-secondary',
              selectedId === t.id && 'bg-secondary',
            )}
          >
            <div className="truncate font-medium">{t.title || `Task ${t.id}`}</div>
            <div className={cn('text-xs', STATUS_COLORS[t.status] || 'text-muted-foreground')}>
              {t.status}
            </div>
          </button>
        </li>
      ))}
    </ul>
  );
}
