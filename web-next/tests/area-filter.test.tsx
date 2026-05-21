import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import {
  AreaFilter,
  visibleAreaCount,
} from '@/components/code-graph/area-filter';

describe('visibleAreaCount', () => {
  it('subtracts the hidden set size from the total', () => {
    const areas = ['agent', 'orchestrator', 'web'];
    expect(visibleAreaCount(areas, new Set())).toBe(3);
    expect(visibleAreaCount(areas, new Set(['agent']))).toBe(2);
    expect(visibleAreaCount(areas, new Set(['agent', 'web']))).toBe(1);
  });

  it('ignores hidden entries that are not in the areas list', () => {
    // Defensive — the hidden set is plumbed from caller state and may
    // briefly carry stale names after a graph refresh.
    expect(
      visibleAreaCount(['agent'], new Set(['agent', 'gone-area'])),
    ).toBe(0);
  });
});

describe('AreaFilter component', () => {
  it('renders the visible/total counter in the toggle label', () => {
    render(<AreaFilter areas={['agent', 'web']} onChange={() => {}} />);
    expect(screen.getByTestId('area-filter-toggle').textContent).toContain(
      '2/2',
    );
  });

  it('emits the toggled set when an area checkbox flips', () => {
    const onChange = vi.fn();
    render(<AreaFilter areas={['agent', 'web']} onChange={onChange} />);
    fireEvent.click(screen.getByTestId('area-filter-toggle'));
    fireEvent.click(screen.getByTestId('area-filter-agent'));
    expect(onChange).toHaveBeenLastCalledWith(new Set(['agent']));
  });

  it('Hide all marks every area as hidden', () => {
    const onChange = vi.fn();
    render(
      <AreaFilter areas={['agent', 'web', 'orchestrator']} onChange={onChange} />,
    );
    fireEvent.click(screen.getByTestId('area-filter-toggle'));
    fireEvent.click(screen.getByTestId('area-filter-hide-all'));
    expect(onChange).toHaveBeenLastCalledWith(
      new Set(['agent', 'web', 'orchestrator']),
    );
  });

  it('Show all clears the hidden set', () => {
    const onChange = vi.fn();
    render(
      <AreaFilter
        areas={['agent', 'web']}
        onChange={onChange}
        initialHidden={new Set(['agent', 'web'])}
      />,
    );
    fireEvent.click(screen.getByTestId('area-filter-toggle'));
    fireEvent.click(screen.getByTestId('area-filter-show-all'));
    expect(onChange).toHaveBeenLastCalledWith(new Set());
  });

  it('shows an empty-state hint when there are no areas', () => {
    render(<AreaFilter areas={[]} onChange={() => {}} />);
    fireEvent.click(screen.getByTestId('area-filter-toggle'));
    expect(screen.getByText(/No areas in this graph/i)).toBeTruthy();
  });
});
