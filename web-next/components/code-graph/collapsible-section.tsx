'use client';
import { useState, type ReactNode } from 'react';
import { Badge } from '@/components/ui/badge';
import { ChevronRight } from 'lucide-react';

interface Props {
  title: string;
  count: number;
  testId?: string;
  defaultOpen?: boolean;
  children: ReactNode;
}

export function CollapsibleSection({
  title,
  count,
  testId,
  defaultOpen = false,
  children,
}: Props) {
  const [open, setOpen] = useState(defaultOpen);
  const bodyId = `${testId ?? 'collapsible'}-body`;
  return (
    <section data-testid={testId} className="rounded-md border bg-card/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={bodyId}
        className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left"
      >
        <span className="flex items-center gap-2 text-sm font-semibold">
          <ChevronRight
            size={14}
            className={`transition-transform ${open ? 'rotate-90' : ''}`}
          />
          {title}
        </span>
        <Badge variant={count > 0 ? 'secondary' : 'outline'}>{count}</Badge>
      </button>
      {open && (
        <div id={bodyId} className="border-t px-3 py-2 text-sm">
          {children}
        </div>
      )}
    </section>
  );
}
