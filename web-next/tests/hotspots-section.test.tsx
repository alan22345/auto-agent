import { describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { HotspotsSection } from '@/components/code-graph/hotspots-section';
import type { Hotspot } from '@/types/api';

const hotspots: Hotspot[] = [
  { file: 'agent/low.py', churn: 0.2, complexity_density: 0.3, score: 0.40, trend: 'cooling' },
  { file: 'orchestrator/router.py', churn: 0.81, complexity_density: 0.9, score: 0.91, trend: 'accelerating' },
];

describe('HotspotsSection', () => {
  it('renders rows sorted by score descending', () => {
    render(<HotspotsSection hotspots={hotspots} />);
    fireEvent.click(screen.getByRole('button'));
    const rows = screen.getAllByTestId('hotspot-row');
    expect(rows[0]).toHaveTextContent('orchestrator/router.py');
    expect(rows[0]).toHaveTextContent('0.91');
    expect(rows[1]).toHaveTextContent('agent/low.py');
  });

  it('renders an empty state', () => {
    render(<HotspotsSection hotspots={[]} />);
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText(/no churn hotspots/i)).toBeInTheDocument();
  });
});
