'use client';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { SearchSession } from '@/lib/search';
import { deleteSession } from '@/lib/search';

export function SessionList({
  sessions,
  activeId,
  onSelect,
  onNew,
  onDeleted,
}: {
  sessions: SearchSession[];
  activeId: number | null;
  onSelect: (id: number) => void;
  onNew: () => void;
  onDeleted: (id: number) => void;
}) {
  return (
    <aside className="flex h-full w-64 flex-col border-r bg-card">
      <div className="p-2">
        <Button className="w-full" onClick={onNew}>+ New search</Button>
      </div>
      <ul className="flex-1 overflow-auto">
        {sessions.map((s) => (
          <li
            key={s.id}
            className={cn(
              'group flex items-center justify-between gap-2 px-3 py-2 text-sm hover:bg-secondary cursor-pointer',
              activeId === s.id && 'bg-secondary',
            )}
            onClick={() => onSelect(s.id)}
          >
            <span className="truncate">{s.title}</span>
            <button
              type="button"
              className="opacity-0 group-hover:opacity-100 text-xs text-muted-foreground hover:text-destructive"
              onClick={async (e) => {
                e.stopPropagation();
                await deleteSession(s.id).catch(() => {});
                onDeleted(s.id);
              }}
            >
              ×
            </button>
          </li>
        ))}
      </ul>
    </aside>
  );
}
