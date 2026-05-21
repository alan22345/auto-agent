'use client';
// 2026-05-21 — bulk per-area hide/reveal control.
//
// Renders a dropdown listing every area in the graph with a checkbox
// per area. Unchecking an area emits the updated ``hiddenAreas`` Set
// upward; ``graph-canvas.tsx`` translates each hidden area name into a
// per-node ``area-hidden`` class so every descendant of the area drops
// out of the canvas, and edges crossing into a hidden area drop too.
//
// The menu is rendered through a React portal to ``document.body``
// with ``position: fixed``. This puts the menu in the document's root
// stacking context with no nested z-index / overflow / transform
// ancestors that could trap or clip it — the cytoscape canvas (which
// owns its own ``z-index:0`` stacking context on the page) cannot
// intercept clicks even if it visually overlaps the menu coordinates.

import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
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

interface MenuPos {
  left: number;
  top: number;
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
  const [menuPos, setMenuPos] = useState<MenuPos | null>(null);
  const [mounted, setMounted] = useState(false);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // Portal target only exists on the client — gate the portal render
  // on a mount flag so SSR sees the toggle button only.
  useEffect(() => {
    setMounted(true);
  }, []);

  // Re-compute the menu's fixed coordinates whenever it opens. The
  // menu drops down from the toggle button: ``left`` aligns with the
  // toggle's left edge, ``top`` sits just below the toggle. Recompute
  // on scroll / resize so the menu doesn't drift away from the button.
  useLayoutEffect(() => {
    if (!open) return;
    const compute = () => {
      const t = toggleRef.current;
      if (!t) return;
      const r = t.getBoundingClientRect();
      setMenuPos({ left: r.left, top: r.bottom + 4 });
    };
    compute();
    window.addEventListener('scroll', compute, true);
    window.addEventListener('resize', compute);
    return () => {
      window.removeEventListener('scroll', compute, true);
      window.removeEventListener('resize', compute);
    };
  }, [open]);

  // Close on click-outside (toggle or menu). Uses the global document
  // mousedown so it works regardless of where in the DOM the menu has
  // been portalled to.
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      const target = e.target as Node | null;
      if (!target) return;
      if (toggleRef.current?.contains(target)) return;
      if (menuRef.current?.contains(target)) return;
      setOpen(false);
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

  const menu = open && menuPos && (
    <div
      ref={menuRef}
      data-testid="area-filter-menu"
      role="listbox"
      style={{
        position: 'fixed',
        left: menuPos.left,
        top: menuPos.top,
        zIndex: 9999,
      }}
      className="w-64 rounded-md border bg-popover text-popover-foreground shadow-md"
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
  );

  return (
    <div className={`relative ${className ?? ''}`}>
      <button
        ref={toggleRef}
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
      {mounted && menu ? createPortal(menu, document.body) : null}
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
