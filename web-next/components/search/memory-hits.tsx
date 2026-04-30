'use client';
import { useState } from 'react';
import type { MemoryHit } from '@/lib/search';

export function MemoryHits({ hits }: { hits: MemoryHit[] }) {
  const [open, setOpen] = useState(true);
  if (hits.length === 0) return null;
  return (
    <div className="mt-3 rounded border bg-card">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-3 py-2 text-sm font-medium hover:bg-secondary"
      >
        <span>From team memory ({hits.length})</span>
        <span className="text-muted-foreground">{open ? '−' : '+'}</span>
      </button>
      {open && (
        <ul className="divide-y">
          {hits.map((h, i) => (
            <li key={`${h.entity.id}-${i}`} className="px-3 py-2 text-sm">
              <div className="font-medium">
                <span className="text-xs uppercase text-muted-foreground mr-2">
                  {h.entity.type}
                </span>
                {h.entity.name}
              </div>
              <ul className="ml-2 mt-1 list-disc pl-4 text-muted-foreground">
                {h.facts.map((f) => (
                  <li key={f.id}>{f.content}</li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
