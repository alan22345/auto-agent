import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { HealthTab } from '@/components/code-graph/health-tab';
import type { RepoGraphBlob } from '@/types/api';

function baseBlob(): RepoGraphBlob {
  return {
    commit_sha: 'abc',
    generated_at: '2026-06-08T00:00:00Z',
    analyser_version: 'phase13-health-0.13.0',
    areas: [],
    nodes: [],
    edges: [],
    public_symbols: [],
    cycles: [],
    dead_code: [],
    clones: [],
    hotspots: [],
    file_health: [
      { file: 'a.py', maintainability_index: 20, band: 'poor', crap: 10 },
    ],
    health: {
      score: 72,
      clone_count: 0,
      cycle_count: 0,
      dead_count: 0,
      hotspot_count: 0,
    },
  };
}

describe('HealthTab', () => {
  it('renders the scorecard and all six sections', () => {
    render(<HealthTab blob={baseBlob()} />);
    expect(screen.getByTestId('health-scorecard')).toBeInTheDocument();
    expect(screen.getByTestId('cycles-section')).toBeInTheDocument();
    expect(screen.getByTestId('dead-code-section')).toBeInTheDocument();
    expect(screen.getByTestId('clones-section')).toBeInTheDocument();
    expect(screen.getByTestId('hotspots-section')).toBeInTheDocument();
    expect(screen.getByTestId('file-health-section')).toBeInTheDocument();
    expect(screen.getByTestId('count-Poor files')).toHaveTextContent('1');
  });

  it('shows a stale banner instead of the scorecard when health is null', () => {
    const blob = baseBlob();
    blob.health = null;
    render(<HealthTab blob={blob} />);
    expect(screen.getByTestId('health-stale')).toBeInTheDocument();
    expect(screen.queryByTestId('health-scorecard')).not.toBeInTheDocument();
    expect(screen.getByTestId('cycles-section')).toBeInTheDocument();
  });

  it('renders without crashing when the quality arrays are absent (pre-quality-layer blob)', () => {
    // A blob produced before the quality layer omits these fields entirely.
    const blob: RepoGraphBlob = {
      commit_sha: 'old',
      generated_at: '2026-01-01T00:00:00Z',
      analyser_version: 'phase2-python-0.2.0',
      areas: [],
      nodes: [],
      edges: [],
    };
    render(<HealthTab blob={blob} />);
    expect(screen.getByTestId('health-stale')).toBeInTheDocument();
    expect(screen.getByTestId('cycles-section')).toBeInTheDocument();
    expect(screen.getByTestId('file-health-section')).toBeInTheDocument();
  });
});
