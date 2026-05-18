import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import CodeGraphPage from '@/app/(app)/code-graph/page';

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe('CodeGraphPage', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the empty state when no graphs are enabled', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => [],
      }),
    );
    wrap(<CodeGraphPage />);
    await waitFor(() => {
      expect(screen.getByText(/No repos are graph-enabled/i)).toBeTruthy();
    });
    // CTA button is rendered in both header + empty state.
    const buttons = screen.getAllByRole('button', { name: /enable for a repo/i });
    expect(buttons.length).toBeGreaterThan(0);
  });

  it('lists configured repos when graphs exist', async () => {
    // OnboardModal also fetches /api/repos via useOrgRepos — return a
    // matching repo so the closed modal renders without warnings.
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (url: string) => {
        if (url.includes('/graph/configs')) {
          return {
            ok: true,
            json: async () => [
              {
                repo_id: 7,
                repo_name: 'demo',
                repo_url: 'https://github.com/example/demo',
                analysis_branch: 'main',
                workspace_path: '/data/graph-workspaces/7',
                last_analysis_id: null,
              },
            ],
          };
        }
        return {
          ok: true,
          json: async () => [{ id: 7, name: 'demo', url: 'https://github.com/example/demo' }],
        };
      }),
    );
    wrap(<CodeGraphPage />);
    await waitFor(() => {
      expect(screen.getByText('demo')).toBeTruthy();
    });
    expect(screen.getByText(/Not analysed yet/i)).toBeTruthy();
    // Branch is rendered.
    expect(screen.getByText('main')).toBeTruthy();
  });

  it('shows an error message when the API fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        statusText: 'Server error',
        json: async () => ({ detail: 'boom' }),
      }),
    );
    wrap(<CodeGraphPage />);
    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeTruthy();
    });
  });
});
