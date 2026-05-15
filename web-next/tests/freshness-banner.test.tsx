import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { FreshnessBanner } from '@/components/code-graph/freshness-banner';
import type { LatestRepoGraphData, RepoGraphBlob } from '@/types/api';

const _BLOB: RepoGraphBlob = {
  commit_sha: 'deadbee1234',
  generated_at: '2026-05-15T12:30:00Z',
  analyser_version: 'phase2-python-0.2.0',
  areas: [],
  nodes: [],
  edges: [],
};

describe('FreshnessBanner', () => {
  it('renders branch + sha7 + generated-at when a blob exists', () => {
    const latest: LatestRepoGraphData = {
      repo_id: 1,
      analysis_branch: 'main',
      repo_graph_id: 1,
      commit_sha: 'deadbee1234567890',
      generated_at: '2026-05-15T12:30:00Z',
      analyser_version: 'phase2-python-0.2.0',
      status: 'ok',
      blob: _BLOB,
    };
    render(<FreshnessBanner latest={latest} />);
    const banner = screen.getByTestId('graph-freshness-banner');
    expect(banner.textContent).toContain('main');
    expect(banner.textContent).toContain('deadbee'); // sha7 prefix
  });

  it('renders the no-analysis-yet message when blob is null', () => {
    const latest: LatestRepoGraphData = {
      repo_id: 1,
      analysis_branch: 'main',
      blob: null,
    };
    render(<FreshnessBanner latest={latest} />);
    expect(screen.getByRole('status').textContent).toMatch(/No analysis yet/i);
  });

  it('surfaces partial-status hint when status is partial', () => {
    const latest: LatestRepoGraphData = {
      repo_id: 1,
      analysis_branch: 'main',
      commit_sha: 'abc1234',
      generated_at: '2026-05-15T12:30:00Z',
      analyser_version: 'x',
      status: 'partial',
      blob: _BLOB,
    };
    render(<FreshnessBanner latest={latest} />);
    expect(screen.getByTestId('graph-freshness-banner').textContent).toMatch(
      /some areas failed/i,
    );
  });
});
