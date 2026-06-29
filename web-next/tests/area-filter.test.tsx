import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { AreaFilter } from '@/components/code-graph/area-filter';

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
