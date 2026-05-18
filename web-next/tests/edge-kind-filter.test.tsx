// ADR-016 Phase 7 §11 — edge-kind filter controls.
//
// Four checkboxes (calls / imports / inherits / http), all checked by
// default. Unchecking a kind emits a new ``hiddenKinds`` Set upward;
// the canvas applies a per-kind cytoscape class with ``display: none``.

import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { EdgeKindFilter } from '@/components/code-graph/edge-kind-filter';
import type { Edge } from '@/types/api';

describe('EdgeKindFilter', () => {
  it('renders all four kind checkboxes, all checked by default', () => {
    render(<EdgeKindFilter onChange={() => {}} />);
    for (const kind of ['calls', 'imports', 'inherits', 'http']) {
      const cb = screen.getByTestId(
        `edge-kind-filter-${kind}`,
      ) as HTMLInputElement;
      expect(cb).toBeTruthy();
      expect(cb.checked).toBe(true);
    }
  });

  it('emits a Set containing the unchecked kind', () => {
    const handler = vi.fn<(s: Set<Edge['kind']>) => void>();
    render(<EdgeKindFilter onChange={handler} />);
    fireEvent.click(screen.getByTestId('edge-kind-filter-calls'));
    expect(handler).toHaveBeenCalledTimes(1);
    const arg = handler.mock.calls[0][0];
    expect(arg.has('calls')).toBe(true);
    expect(arg.size).toBe(1);
  });

  it('accumulates multiple unchecked kinds', () => {
    const handler = vi.fn<(s: Set<Edge['kind']>) => void>();
    render(<EdgeKindFilter onChange={handler} />);
    fireEvent.click(screen.getByTestId('edge-kind-filter-calls'));
    fireEvent.click(screen.getByTestId('edge-kind-filter-imports'));
    fireEvent.click(screen.getByTestId('edge-kind-filter-http'));
    const last = handler.mock.calls[handler.mock.calls.length - 1][0];
    expect(last.has('calls')).toBe(true);
    expect(last.has('imports')).toBe(true);
    expect(last.has('http')).toBe(true);
    expect(last.has('inherits')).toBe(false);
    expect(last.size).toBe(3);
  });

  it('re-checking a kind removes it from the hidden set', () => {
    const handler = vi.fn<(s: Set<Edge['kind']>) => void>();
    render(<EdgeKindFilter onChange={handler} />);
    fireEvent.click(screen.getByTestId('edge-kind-filter-calls'));
    fireEvent.click(screen.getByTestId('edge-kind-filter-calls'));
    const last = handler.mock.calls[handler.mock.calls.length - 1][0];
    expect(last.size).toBe(0);
  });

  it('respects an initial hidden set', () => {
    render(
      <EdgeKindFilter
        initialHidden={new Set(['imports'])}
        onChange={() => {}}
      />,
    );
    const imports = screen.getByTestId(
      'edge-kind-filter-imports',
    ) as HTMLInputElement;
    expect(imports.checked).toBe(false);
    const calls = screen.getByTestId(
      'edge-kind-filter-calls',
    ) as HTMLInputElement;
    expect(calls.checked).toBe(true);
  });
});
