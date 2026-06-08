import { describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { DeadCodeSection } from '@/components/code-graph/dead-code-section';
import type { DeadCodeFinding } from '@/types/api';

const dead: DeadCodeFinding[] = [
  { kind: 'unused_export', target: 'agent/x.py::foo', file: 'agent/x.py', reason: 'no importers' },
];

describe('DeadCodeSection', () => {
  it('renders a row per finding when expanded', () => {
    render(<DeadCodeSection deadCode={dead} />);
    fireEvent.click(screen.getByRole('button'));
    const row = screen.getByTestId('dead-code-row');
    expect(row).toHaveTextContent('unused_export');
    expect(row).toHaveTextContent('agent/x.py::foo');
    expect(row).toHaveTextContent('no importers');
  });

  it('renders an empty state', () => {
    render(<DeadCodeSection deadCode={[]} />);
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText(/no dead code/i)).toBeInTheDocument();
  });
});
