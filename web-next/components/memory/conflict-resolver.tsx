'use client';
import type { MemoryRow } from '@/types/ws';

const CHOICES: { value: MemoryRow['resolution']; label: string }[] = [
  { value: 'keep_existing', label: 'Keep existing (skip this row)' },
  { value: 'replace', label: 'Replace existing with new (correct)' },
  { value: 'keep_both', label: 'Keep both' },
];

export function ConflictResolver({
  row,
  index,
  updateRow,
}: {
  row: MemoryRow;
  index: number;
  updateRow: (i: number, patch: Partial<MemoryRow>) => void;
}) {
  return (
    <div className="mt-2 rounded border border-destructive/30 bg-background/50 p-2 text-xs">
      <div className="mb-1 font-medium">⚠ Conflict with existing fact(s):</div>
      {row.conflicts.map((c, j) => (
        <div key={j} className="mb-1 text-muted-foreground">
          <em>existing:</em> {c.existing_content}
        </div>
      ))}
      <div className="mt-1 flex flex-col gap-1">
        {CHOICES.map((c) => (
          <label key={String(c.value)} className="flex items-center gap-2">
            <input
              type="radio"
              name={`res-${index}`}
              checked={row.resolution === c.value}
              onChange={() => updateRow(index, { resolution: c.value })}
            />
            <span>{c.label}</span>
          </label>
        ))}
      </div>
    </div>
  );
}
