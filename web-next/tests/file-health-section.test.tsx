import { describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { FileHealthSection } from '@/components/code-graph/file-health-section';
import type { FileHealth } from '@/types/api';

const fileHealth: FileHealth[] = [
  { file: 'agent/good.py', maintainability_index: 80.5, band: 'good', crap: 2.0 },
  { file: 'orchestrator/router.py', maintainability_index: 30.1, band: 'poor', crap: 41.5 },
];

describe('FileHealthSection', () => {
  it('renders rows sorted by maintainability index ascending (worst first)', () => {
    render(<FileHealthSection fileHealth={fileHealth} />);
    fireEvent.click(screen.getByRole('button'));
    const rows = screen.getAllByTestId('file-health-row');
    expect(rows[0]).toHaveTextContent('orchestrator/router.py');
    expect(rows[0]).toHaveTextContent('poor');
    expect(rows[1]).toHaveTextContent('agent/good.py');
  });

  it('renders an empty state', () => {
    render(<FileHealthSection fileHealth={[]} />);
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText(/no file-health records/i)).toBeInTheDocument();
  });
});
