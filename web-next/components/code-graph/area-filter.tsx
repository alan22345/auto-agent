'use client';
// 2026-05-21 — bulk per-area hide/reveal control.
//
// Renders a dropdown listing every area in the graph with a checkbox
// per area. Unchecking an area emits the updated ``hiddenAreas`` Set
// upward; ``graph-canvas.tsx`` translates each hidden area name into a
// per-node ``area-hidden`` class so every descendant of the area drops
// out of the canvas, and edges crossing into a hidden area drop too.
//
// Mirrors ``edge-kind-filter.tsx`` in shape: uncontrolled, owns its
// own state, emits on every toggle. A "Show all" / "Hide all" pair
// short-circuits the most common cases for graphs with many areas
// (cardamon has 19).

import { useEffect, useRef, useState } from 'react';
import { ChevronDown } from 'lucide-react';

interface Props {
  /** Area names to render as checkboxes. Order is preserved. */
  areas: string[];
  /** Called with the new hidden set on every toggle / bulk action. */
  onChange: (hidden: Set<string>) => void;
  /** Optional seed — areas in this set start unchecked. */
  initialHidden?: Set<string>;
  className?: string;
}

export function AreaFilter({
  areas,
  onChange,
  initialHidden,
  className,
}: Props) {
  const [hidden, setHidden] = useState<Set<string>>(
    () => new Set(initialHidden ?? []),
  );
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  // Close the dropdown when the user clicks outside it. Keyboard
  // dismissal (Escape) is also handy for a11y.
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (!rootRef.current) return;
      if (!rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const emit = (next: Set<string>) => {
    setHidden(next);
    onChange(next);
  };

  const toggle = (name: string) => {
    const next = new Set(hidden);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    emit(next);
  };

  const showAll = () => emit(new Set());
  const hideAll = () => emit(new Set(areas));

  const visibleCount = areas.length - hidden.size;

  return (
    <div ref={rootRef} className={`relative ${className ?? ''}`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        data-testid="area-filter-toggle"
        aria-haspopup="listbox"
        aria-expanded={open}
        className="inline-flex h-7 items-center gap-1 rounded-md border bg-card/95 px-2 text-xs shadow-sm hover:bg-card"
      >
        <span>
          Areas {visibleCount}/{areas.length}
        </span>
        <ChevronDown size={12} />
      </button>
      {open && (
        <div
          data-testid="area-filter-menu"
          // ``z-50`` is required to lift the dropdown above the
          // cytoscape canvas, which spawns its own stacking context
          // and otherwise tunnels clicks through to graph nodes when
          // the menu visually overlaps the canvas (e.g. the menu drops
          // down from the toolbar over the graph area below).
          className="absolute left-0 z-50 mt-1 w-64 rounded-md border bg-popover text-popover-foreground shadow-md"
          role="listbox"
        >
          <div className="flex items-center justify-between border-b px-2 py-1.5 text-[11px]">
            <button
              type="button"
              data-testid="area-filter-show-all"
              onClick={showAll}
              className="underline-offset-2 hover:underline"
            >
              Show all
            </button>
            <button
              type="button"
              data-testid="area-filter-hide-all"
              onClick={hideAll}
              className="underline-offset-2 hover:underline"
            >
              Hide all
            </button>
          </div>
          <div className="max-h-72 overflow-y-auto px-2 py-1">
            {areas.map((name) => {
              const checked = !hidden.has(name);
              return (
                <label
                  key={name}
                  className="flex cursor-pointer items-center gap-2 py-1 text-xs"
                >
                  <input
                    type="checkbox"
                    data-testid={`area-filter-${name}`}
                    checked={checked}
                    onChange={() => toggle(name)}
                    className="h-3.5 w-3.5 cursor-pointer accent-primary"
                  />
                  <span className="truncate">{name}</span>
                </label>
              );
            })}
            {areas.length === 0 && (
              <p className="px-1 py-2 text-xs text-muted-foreground">
                No areas in this graph.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Pure helper — given a flat list of area names and a hidden set,
 * return the count of areas that are currently visible. Useful for
 * tests + future toolbar variants.
 */
export function visibleAreaCount(
  areas: string[],
  hidden: Set<string>,
): number {
  let n = 0;
  for (const a of areas) if (!hidden.has(a)) n += 1;
  return n;
}
