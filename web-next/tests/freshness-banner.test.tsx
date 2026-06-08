import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { FreshnessBanner } from '@/components/code-graph/freshness-banner';
import type {
  GraphStalenessResponse,
  LatestRepoGraphData,
  RepoGraphBlob,
} from '@/types/api';

const _BLOB: RepoGraphBlob = {
  commit_sha: 'deadbee1234',
  generated_at: '2026-05-15T12:30:00Z',
  analyser_version: 'phase2-python-0.2.0',
  areas: [],
  nodes: [],
  edges: [],
};

const FULL_SHA = 'deadbee1234567890abcdef1234567890abcdef1';

function _latest(
  overrides: Partial<LatestRepoGraphData> = {},
): LatestRepoGraphData {
  return {
    repo_id: 1,
    analysis_branch: 'main',
    repo_graph_id: 1,
    commit_sha: FULL_SHA,
    generated_at: '2026-05-15T12:30:00Z',
    analyser_version: 'phase7-partial-0.5.0',
    status: 'ok',
    blob: _BLOB,
    is_complete: true,
    processed_files_count: 0,
    total_files_estimate: 0,
    ...overrides,
  };
}

describe('FreshnessBanner', () => {
  it('renders branch + sha7 + generated-at when a blob exists', () => {
    render(<FreshnessBanner latest={_latest()} />);
    const banner = screen.getByTestId('graph-freshness-banner');
    expect(banner.textContent).toContain('main');
    expect(banner.textContent).toContain('deadbee'); // sha7 prefix
  });

  it('renders the no-analysis-yet message when blob is null', () => {
    const latest: LatestRepoGraphData = {
      repo_id: 1,
      analysis_branch: 'main',
      blob: null,
      is_complete: true,
      processed_files_count: 0,
      total_files_estimate: 0,
    };
    render(<FreshnessBanner latest={latest} />);
    expect(screen.getByRole('status').textContent).toMatch(/No analysis yet/i);
  });

  it('surfaces partial-status hint when status is partial', () => {
    render(
      <FreshnessBanner
        latest={_latest({ status: 'partial', commit_sha: 'abc1234' })}
      />,
    );
    expect(screen.getByTestId('graph-freshness-banner').textContent).toMatch(
      /some areas failed/i,
    );
  });

  // --- ADR-016 Phase 7 §11 — banner polish ---

  it('exposes the full sha via a title attribute on the sha span', () => {
    // ADR-016 Phase 7 §11 — hovering the short sha must surface the
    // full 40-char commit SHA so a user can copy the canonical id.
    render(<FreshnessBanner latest={_latest()} />);
    const shaEl = screen.getByTestId('graph-freshness-sha');
    expect(shaEl.textContent).toBe('deadbee');
    expect(shaEl.getAttribute('title')).toBe(FULL_SHA);
  });

  it('renders the analyser version', () => {
    render(<FreshnessBanner latest={_latest()} />);
    const banner = screen.getByTestId('graph-freshness-banner');
    expect(banner.textContent).toContain('phase7-partial-0.5.0');
  });

  it('renders the stale warning when staleness.drifted === true', () => {
    const staleness: GraphStalenessResponse = {
      graph_sha: FULL_SHA,
      workspace_sha: 'f00ba12' + 'a'.repeat(33),
      drifted: true,
    };
    render(<FreshnessBanner latest={_latest()} staleness={staleness} />);
    expect(screen.getByTestId('graph-stale-warning').textContent).toMatch(
      /workspace has moved/i,
    );
  });

  it('does NOT render the stale warning when drifted === false', () => {
    const staleness: GraphStalenessResponse = {
      graph_sha: FULL_SHA,
      workspace_sha: FULL_SHA,
      drifted: false,
    };
    render(<FreshnessBanner latest={_latest()} staleness={staleness} />);
    expect(screen.queryByTestId('graph-stale-warning')).toBeNull();
  });

  it('does NOT render the stale warning when staleness is missing', () => {
    render(<FreshnessBanner latest={_latest()} />);
    expect(screen.queryByTestId('graph-stale-warning')).toBeNull();
  });
});
