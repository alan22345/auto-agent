'use client';
import { useState } from 'react';
import type { Source } from '@/lib/search';

export function SourceList({ sources }: { sources: Source[] }) {
  const [open, setOpen] = useState(true);
  if (sources.length === 0) return null;
  return (
    <div className="mt-3 rounded border bg-card">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-3 py-2 text-sm font-medium hover:bg-secondary"
      >
        <span>Sources ({sources.length})</span>
        <span className="text-muted-foreground">{open ? '−' : '+'}</span>
      </button>
      {open && (
        <ul className="divide-y">
          {sources.map((s, i) => (
            <li key={`${s.url}-${i}`} className="px-3 py-2 text-sm">
              <div className="flex items-baseline justify-between gap-2">
                <a
                  href={s.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="font-medium text-primary hover:underline"
                >
                  [{i + 1}] {s.title}
                </a>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {hostname(s.url)}
                </span>
              </div>
              {s.summary && (
                <p className="mt-1 line-clamp-2 text-muted-foreground">{s.summary}</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function hostname(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, ''); }
  catch { return url; }
}
