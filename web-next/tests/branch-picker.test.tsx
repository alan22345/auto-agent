import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BranchPicker } from '@/components/code-graph/branch-picker';
import type { RepoGraphConfigData } from '@/types/api';

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const cfg: RepoGraphConfigData = {
  repo_id: 1,
  repo_name: 'demo',
  repo_url: 'https://github.com/example/demo',
  analysis_branch: 'main',
  workspace_path: '/data/graph-workspaces/1',
  analyser_version: '',
  last_analysis_id: null,
};

describe('BranchPicker', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('persists the new branch via the API', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ...cfg, analysis_branch: 'release' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    wrap(<BranchPicker config={cfg} />);

    const input = screen.getByLabelText(/Analysis branch/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'release' } });
    fireEvent.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
    const call = fetchMock.mock.calls[0];
    expect(call[0]).toBe('/api/repos/1/graph');
    expect(call[1].method).toBe('PATCH');
    expect(JSON.parse(call[1].body)).toEqual({ analysis_branch: 'release' });

    await waitFor(() => {
      expect(screen.getByText(/Saved/i)).toBeTruthy();
    });
  });

  it('does not call the API when the branch is unchanged', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    wrap(<BranchPicker config={cfg} />);
    fireEvent.click(screen.getByRole('button', { name: /save/i }));
    // give the form a microtask to run
    await new Promise((r) => setTimeout(r, 5));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('surfaces API errors', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 400,
        statusText: 'Bad request',
        json: async () => ({ detail: 'Invalid branch name' }),
      }),
    );

    wrap(<BranchPicker config={cfg} />);
    const input = screen.getByLabelText(/Analysis branch/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'release/v2' } });
    fireEvent.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeTruthy();
    });
    expect(screen.getByRole('alert').textContent).toContain('Invalid branch name');
  });
});
