'use client';
// ADR-016 Phase 7 §11 — edge-kind toggle controls.
//
// Four checkboxes: calls / imports / inherits / http. All checked by
// default so the user sees the full graph on load. Unchecking a kind
// emits the new ``hiddenKinds`` Set upward; the parent page lifts the
// set into a prop on ``GraphCanvas`` which translates each kind into a
// per-kind cytoscape class with ``display: none``.
//
// The component is uncontrolled (owns its own state) and emits on
// every change — the parent can either trust the emitted set as a
// snapshot or seed the initial state through ``initialHidden``.

import { useState } from 'react';
import type { Edge } from '@/types/api';

export const EDGE_KINDS: Edge['kind'][] = ['calls', 'imports', 'inherits', 'http'];

interface Props {
  onChange: (hidden: Set<Edge['kind']>) => void;
  /** Optional seed — kinds in this set start unchecked. Defaults to
   * empty (all kinds visible). */
  initialHidden?: Set<Edge['kind']>;
  className?: string;
}

export function EdgeKindFilter({
  onChange,
  initialHidden,
  className,
}: Props) {
  const [hidden, setHidden] = useState<Set<Edge['kind']>>(
    () => new Set(initialHidden ?? []),
  );

  const toggle = (kind: Edge['kind']) => {
    const next = new Set(hidden);
    if (next.has(kind)) next.delete(kind);
    else next.add(kind);
    setHidden(next);
    onChange(next);
  };

  return (
    <fieldset
      className={`flex items-center gap-3 text-xs ${className ?? ''}`}
      data-testid="edge-kind-filter"
    >
      <legend className="sr-only">Edge kinds</legend>
      {EDGE_KINDS.map((kind) => {
        const checked = !hidden.has(kind);
        return (
          <label
            key={kind}
            className="inline-flex cursor-pointer items-center gap-1 select-none"
          >
            <input
              type="checkbox"
              data-testid={`edge-kind-filter-${kind}`}
              checked={checked}
              onChange={() => toggle(kind)}
              className="h-3.5 w-3.5 cursor-pointer accent-primary"
            />
            <span className="capitalize">{kind}</span>
          </label>
        );
      })}
    </fieldset>
  );
}
