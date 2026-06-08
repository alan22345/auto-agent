import { describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ClonesSection } from '@/components/code-graph/clones-section';
import type { CloneGroup } from '@/types/api';

const clones: CloneGroup[] = [
  {
    id: 'g1',
    token_len: 120,
    mode: 'strict',
    family_id: null,
    instances: [
      { node_id: 'a', file: 'agent/a.py', line_start: 10, line_end: 30 },
      { node_id: 'b', file: 'agent/b.py', line_start: 5, line_end: 25 },
    ],
  },
];

describe('ClonesSection', () => {
  it('renders the family with its instance locations', () => {
    render(<ClonesSection clones={clones} />);
    fireEvent.click(screen.getByRole('button'));
    const row = screen.getByTestId('clone-row');
    expect(row).toHaveTextContent('strict');
    expect(row).toHaveTextContent('agent/a.py:10-30');
    expect(row).toHaveTextContent('agent/b.py:5-25');
  });

  it('renders an empty state', () => {
    render(<ClonesSection clones={[]} />);
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText(/no code clones/i)).toBeInTheDocument();
  });
});
