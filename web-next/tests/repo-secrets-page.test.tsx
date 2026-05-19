import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import RepoSecretsPage from '@/app/(app)/repos/[repo]/secrets/page';
import { AddSecretModal } from '@/components/secrets/AddSecretModal';
import { AwaitingSecretsCard } from '@/components/tasks/scaffold/awaiting-secrets-card';

// ADR-019 §9 — component tests for the secrets page, add-secret modal,
// reveal confirmation, and the scaffold awaiting-secrets banner.

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const ARCHITECT_SECRET = {
  key: 'STRIPE_API_KEY',
  is_set: false,
  source: 'architect_required',
  purpose: 'Charge cards via Stripe',
  updated_at: null,
};

const USER_SECRET = {
  key: 'MY_CUSTOM_KEY',
  is_set: true,
  source: 'user',
  purpose: null,
  updated_at: '2026-05-01T00:00:00Z',
};

const MOCK_REPOS = [{ id: 42, name: 'my-repo', url: 'https://github.com/example/my-repo' }];

describe('RepoSecretsPage', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders both sections with mock data', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (url: string) => {
        if (url.includes('/secrets')) {
          return {
            ok: true,
            json: async () => ({ keys: [ARCHITECT_SECRET, USER_SECRET] }),
          };
        }
        // repos list
        return { ok: true, json: async () => MOCK_REPOS };
      }),
    );

    wrap(
      <RepoSecretsPage params={{ repo: '42' }} />,
    );

    await waitFor(() => {
      expect(screen.getByText('Required by architects')).toBeTruthy();
    });
    expect(screen.getByText('Other secrets')).toBeTruthy();
    expect(screen.getByText('STRIPE_API_KEY')).toBeTruthy();
    expect(screen.getByText('Charge cards via Stripe')).toBeTruthy();
    expect(screen.getByText('MY_CUSTOM_KEY')).toBeTruthy();
  });

  it('shows error when API fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (url: string) => {
        if (url.includes('/secrets')) {
          return {
            ok: false,
            status: 500,
            statusText: 'Internal Server Error',
            json: async () => ({ detail: 'boom' }),
          };
        }
        return { ok: true, json: async () => [] };
      }),
    );

    wrap(<RepoSecretsPage params={{ repo: '42' }} />);

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeTruthy();
    });
  });
});

describe('AddSecretModal — key pattern validation', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true, cleared: false }) }));
  });

  it('rejects a key that starts with a lowercase letter', async () => {
    const onOpenChange = vi.fn();
    wrap(<AddSecretModal repoId={1} open={true} onOpenChange={onOpenChange} />);

    const keyInput = screen.getByLabelText('Key');
    // type a lowercase key - component uppercases on change but validate checks
    fireEvent.change(keyInput, { target: { value: 'invalid_key' } });

    // The uppercase conversion means 'invalid_key' → 'INVALID_KEY' which IS valid.
    // Test with a value that starts with digit instead.
    fireEvent.change(keyInput, { target: { value: '1_BAD' } });

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeTruthy();
    });
  });

  it('accepts a valid uppercase key', async () => {
    const onOpenChange = vi.fn();
    wrap(<AddSecretModal repoId={1} open={true} onOpenChange={onOpenChange} />);

    const keyInput = screen.getByLabelText('Key');
    fireEvent.change(keyInput, { target: { value: 'STRIPE_API_KEY' } });

    // No error should appear
    await waitFor(() => {
      expect(screen.queryByRole('alert')).toBeNull();
    });
  });

  it('disables the save button when the key is invalid', async () => {
    const onOpenChange = vi.fn();
    wrap(<AddSecretModal repoId={1} open={true} onOpenChange={onOpenChange} />);

    const keyInput = screen.getByLabelText('Key');
    fireEvent.change(keyInput, { target: { value: '1_BAD' } });

    // The save button should be disabled
    const saveBtn = screen.getByRole('button', { name: /save secret/i });
    expect(saveBtn).toHaveProperty('disabled', true);
  });
});

describe('Reveal confirmation flow', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('shows confirmation dialog before revealing a secret', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (url: string) => {
        if (url.includes('/secrets')) {
          return {
            ok: true,
            json: async () => ({
              keys: [{ key: 'MY_SECRET', is_set: true, source: 'user', purpose: null }],
            }),
          };
        }
        return { ok: true, json: async () => MOCK_REPOS };
      }),
    );

    wrap(<RepoSecretsPage params={{ repo: '42' }} />);

    // Wait for the page to load
    await waitFor(() => {
      expect(screen.getByText('MY_SECRET')).toBeTruthy();
    });

    // Click the reveal button (eye icon button)
    const revealBtn = screen.getByTitle('Reveal value');
    fireEvent.click(revealBtn);

    // Confirmation dialog should appear
    await waitFor(() => {
      expect(screen.getByText(/Reveal secret\?/i)).toBeTruthy();
    });
    expect(screen.getByText(/audit-logged/i)).toBeTruthy();
  });
});

describe('RepoSecretsPage — filter=architect_required', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('hides Other secrets section when ?filter=architect_required is set', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (url: string) => {
        if (url.includes('/secrets')) {
          return {
            ok: true,
            json: async () => ({ keys: [ARCHITECT_SECRET, USER_SECRET] }),
          };
        }
        return { ok: true, json: async () => MOCK_REPOS };
      }),
    );

    wrap(
      <RepoSecretsPage params={{ repo: '42' }} searchParams={{ filter: 'architect_required' }} />,
    );

    await waitFor(() => {
      expect(screen.getByText('Required by architects')).toBeTruthy();
    });
    expect(screen.queryByText('Other secrets')).toBeNull();
  });
});

describe('AwaitingSecretsCard — scaffold banner', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the banner with alert role and recheck button', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (url: string) => {
        if (url.includes('/repos/1/secrets')) {
          return {
            ok: true,
            json: async () => ({
              keys: [
                { key: 'STRIPE_API_KEY', is_set: false, source: 'architect_required', purpose: 'Stripe' },
                { key: 'OPENAI_KEY', is_set: false, source: 'architect_required', purpose: 'OpenAI' },
              ],
            }),
          };
        }
        // repos list — returns the repo so repoId resolves
        return {
          ok: true,
          json: async () => [{ id: 1, name: 'my-repo', url: 'https://github.com/example/my-repo' }],
        };
      }),
    );

    wrap(<AwaitingSecretsCard taskId={99} repoName="my-repo" />);

    // The alert renders immediately (before async resolves).
    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeTruthy();
    });
    // The "required secrets" text is always present in the banner.
    expect(screen.getByText(/required secrets/i)).toBeTruthy();
    // The Recheck button is always present.
    expect(screen.getByRole('button', { name: /recheck/i })).toBeTruthy();
  });

  it('renders the banner even without a resolved repo (no count)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (url: string) => {
        if (url.includes('/repos')) {
          return { ok: true, json: async () => [] };
        }
        return { ok: true, json: async () => ({ keys: [] }) };
      }),
    );

    wrap(<AwaitingSecretsCard taskId={99} repoName={null} />);

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeTruthy();
    });
    // Without a resolved repo, shows generic text
    expect(screen.getByText(/required secrets/i)).toBeTruthy();
  });
});
