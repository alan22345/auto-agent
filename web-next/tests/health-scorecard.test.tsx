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
  maintainability: 88,
  duplication: 64,
  dead_code: 70,
  cycles: 95,
  coupling: 55,
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

  it('renders a bar + plain-language explanation for each sub-score', () => {
    render(<HealthScorecard health={health} poorFileCount={4} />);
    for (const label of [
      'Maintainability',
      'Duplication',
      'Dead code',
      'Cycles',
      'Coupling',
    ]) {
      expect(screen.getByTestId(`subscore-${label}`)).toBeInTheDocument();
    }
    // value + explanation text are shown
    expect(screen.getByTestId('subscore-Maintainability')).toHaveTextContent('88');
    expect(
      screen.getByTestId('subscore-Duplication'),
    ).toHaveTextContent(/copy-pasted/i);
  });

  it('omits sub-score bars on a pre-composite blob (fields absent)', () => {
    const legacy: RepoHealth = {
      score: 80,
      clone_count: 0,
      cycle_count: 0,
      dead_count: 0,
      hotspot_count: 0,
    };
    render(<HealthScorecard health={legacy} poorFileCount={0} />);
    expect(screen.queryByTestId('subscore-Maintainability')).not.toBeInTheDocument();
  });
});
