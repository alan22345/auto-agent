import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AreaRefreshOverlay } from '@/components/code-graph/area-refresh-overlay';
import type { RepoGraphBlob } from '@/types/api';

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const blob: RepoGraphBlob = {
  commit_sha: 'abc',
  generated_at: '2026-05-15T00:00:00Z',
  analyser_version: 'phase7-0.7.0',
  areas: [
    { name: 'agent', status: 'ok', error: null, unresolved_dynamic_sites: 0 },
    { name: 'orchestrator', status: 'ok', error: null, unresolved_dynamic_sites: 0 },
  ],
  nodes: [
    {
      id: 'area:agent',
      kind: 'area',
      label: 'agent',
      file: null,
      line_start: null,
      line_end: null,
      area: 'agent',
      parent: null,
    },
    {
      id: 'area:orchestrator',
      kind: 'area',
      label: 'orchestrator',
      file: null,
      line_start: null,
      line_end: null,
      area: 'orchestrator',
      parent: null,
    },
  ],
  edges: [],
};

describe('AreaRefreshOverlay', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders one button per area in the blob', () => {
    wrap(
      <AreaRefreshOverlay
        repoId={7}
        blob={blob}
        cy={null}
        layoutTick={0}
      />,
    );
    expect(screen.getByTestId('area-refresh-agent')).toBeTruthy();
    expect(screen.getByTestId('area-refresh-orchestrator')).toBeTruthy();
  });

  it('POSTs to /api/repos/{repoId}/graph/refresh?area=<name> on click', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ request_id: 'req-12345678', status: 'accepted' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    wrap(
      <AreaRefreshOverlay
        repoId={7}
        blob={blob}
        cy={null}
        layoutTick={0}
      />,
    );

    fireEvent.click(screen.getByTestId('area-refresh-agent'));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/repos/7/graph/refresh?area=agent');
    expect(init.method).toBe('POST');
  });

  it('url-encodes area names that contain special characters', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ request_id: 'req', status: 'accepted' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const odd: RepoGraphBlob = {
      ...blob,
      nodes: [
        {
          id: 'area:my.area_x-1',
          kind: 'area',
          label: 'my.area_x-1',
          file: null,
          line_start: null,
          line_end: null,
          area: 'my.area_x-1',
          parent: null,
        },
      ],
      areas: [
        {
          name: 'my.area_x-1',
          status: 'ok',
          error: null,
          unresolved_dynamic_sites: 0,
        },
      ],
    };

    wrap(<AreaRefreshOverlay repoId={3} blob={odd} cy={null} layoutTick={0} />);
    fireEvent.click(screen.getByTestId('area-refresh-my.area_x-1'));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url] = fetchMock.mock.calls[0] as [string];
    // encodeURIComponent passes ``.``, ``_``, ``-`` through verbatim.
    expect(url).toBe('/api/repos/3/graph/refresh?area=my.area_x-1');
  });

  it('emits no buttons when the blob has no area nodes', () => {
    const empty: RepoGraphBlob = {
      ...blob,
      areas: [],
      nodes: [],
    };
    wrap(
      <AreaRefreshOverlay
        repoId={7}
        blob={empty}
        cy={null}
        layoutTick={0}
      />,
    );
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });
});
