'use client';
import { useMemo, useState } from 'react';
import type { MemoryEntitySummary } from '@/types/api';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';

interface Props {
  entities: MemoryEntitySummary[];
  onSelect: (name: string) => void;
}

const TYPE_ORDER = ['project', 'concept', 'person', 'repo', 'system', 'decision'];

export function EntityList({ entities, onSelect }: Props) {
  const groups = useMemo(() => {
    const byType = new Map<string, MemoryEntitySummary[]>();
    for (const e of entities) {
      const arr = byType.get(e.type) ?? [];
      arr.push(e);
      byType.set(e.type, arr);
    }
    const ordered: [string, MemoryEntitySummary[]][] = [];
    for (const t of TYPE_ORDER) if (byType.has(t)) ordered.push([t, byType.get(t)!]);
    for (const [t, arr] of byType) if (!TYPE_ORDER.includes(t)) ordered.push([t, arr]);
    return ordered;
  }, [entities]);

  if (entities.length === 0) {
    return <p className="px-2 py-6 text-center text-xs text-muted-foreground">No entities.</p>;
  }

  return (
    <div className="flex flex-col gap-3">
      {groups.map(([type, items]) => (
        <Group key={type} type={type} items={items} onSelect={onSelect} />
      ))}
    </div>
  );
}

function Group({
  type,
  items,
  onSelect,
}: {
  type: string;
  items: MemoryEntitySummary[];
  onSelect: (name: string) => void;
}) {
  const [open, setOpen] = useState(true);
  return (
    <section>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="mb-1 flex w-full items-center justify-between text-xs uppercase tracking-wide text-muted-foreground hover:text-foreground"
      >
        <span>
          {type} <span className="ml-1 normal-case text-muted-foreground/70">({items.length})</span>
        </span>
        <span aria-hidden>{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <ul className="flex flex-col gap-1">
          {items.map((e) => (
            <li key={e.id || e.name}>
              <button
                type="button"
                onClick={() => onSelect(e.name)}
                className={cn(
                  'w-full rounded border bg-card p-2 text-left text-sm transition-colors hover:bg-accent/40',
                )}
              >
                <div className="flex items-start justify-between gap-2">
                  <span className="font-medium">{e.name}</span>
                  <Badge variant="secondary" className="shrink-0 text-[10px]">
                    {e.fact_count}
                  </Badge>
                </div>
                {e.tags && e.tags.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1">
                    {e.tags.slice(0, 6).map((t) => (
                      <Badge key={t} variant="outline" className="text-[10px]">
                        {t}
                      </Badge>
                    ))}
                  </div>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
