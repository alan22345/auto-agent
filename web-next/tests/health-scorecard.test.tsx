import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { HealthScorecard } from '@/components/code-graph/health-scorecard';
import type { RepoHealth } from '@/types/api';

const health: RepoHealth = {
  score: 72.4,
  clone_count: 5,
  cycle_count: 3,
  dead_count: 8,
  hotspot_count: 12,
};

describe('HealthScorecard', () => {
  it('renders the rounded score and all counts', () => {
    render(<HealthScorecard health={health} poorFileCount={4} />);
    expect(screen.getByTestId('health-score')).toHaveTextContent('72');
    expect(screen.getByTestId('count-Cycles')).toHaveTextContent('3');
    expect(screen.getByTestId('count-Clones')).toHaveTextContent('5');
    expect(screen.getByTestId('count-Dead code')).toHaveTextContent('8');
    expect(screen.getByTestId('count-Hotspots')).toHaveTextContent('12');
    expect(screen.getByTestId('count-Poor files')).toHaveTextContent('4');
  });
});
