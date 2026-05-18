// ADR-016 Phase 7 §11 — search input for the graph canvas.
//
// The input is debounced (300ms) so rapid typing doesn't churn the
// cytoscape side effect that recomputes the node fade classes. The
// component is otherwise dumb — state lives on the page so the canvas
// + toolbar share it.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, act } from '@testing-library/react';
import { SearchInput } from '@/components/code-graph/search-input';

describe('SearchInput', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('debounces onChange — only one call after 300ms of rapid typing', () => {
    const handler = vi.fn();
    render(<SearchInput onChange={handler} />);
    const input = screen.getByTestId('graph-search-input') as HTMLInputElement;

    // Type four characters in rapid succession.
    fireEvent.change(input, { target: { value: 'a' } });
    fireEvent.change(input, { target: { value: 'ag' } });
    fireEvent.change(input, { target: { value: 'age' } });
    fireEvent.change(input, { target: { value: 'agen' } });

    // Nothing fires before the debounce window elapses.
    expect(handler).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(299);
    });
    expect(handler).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(1);
    });
    // After the window, exactly one call with the final value.
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenLastCalledWith('agen');
  });

  it('emits an empty string when the input is cleared', () => {
    const handler = vi.fn();
    render(<SearchInput onChange={handler} />);
    const input = screen.getByTestId('graph-search-input') as HTMLInputElement;

    fireEvent.change(input, { target: { value: 'agent' } });
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(handler).toHaveBeenLastCalledWith('agent');

    fireEvent.change(input, { target: { value: '' } });
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(handler).toHaveBeenLastCalledWith('');
  });

  it('respects an explicit initial value', () => {
    const handler = vi.fn();
    render(<SearchInput onChange={handler} initialValue="prefilled" />);
    const input = screen.getByTestId('graph-search-input') as HTMLInputElement;
    expect(input.value).toBe('prefilled');
    // Initial value does NOT trigger a debounced onChange — that would
    // double-fire when the parent already has the same state.
    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(handler).not.toHaveBeenCalled();
  });
});
