import { describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { CyclesSection } from '@/components/code-graph/cycles-section';
import type { DependencyCycle } from '@/types/api';

const cycles: DependencyCycle[] = [
  { id: 'c1', kind: 'import', members: ['agent/x', 'agent/y'], closing_edges: [] },
];

describe('CyclesSection', () => {
  it('renders the member chain when expanded', () => {
    render(<CyclesSection cycles={cycles} />);
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByTestId('cycle-row')).toHaveTextContent('agent/x → agent/y');
  });

  it('renders an empty state with zero cycles', () => {
    render(<CyclesSection cycles={[]} />);
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText(/no dependency cycles/i)).toBeInTheDocument();
  });
});
