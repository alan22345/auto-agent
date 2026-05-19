'use client';

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode, FC } from 'react';
import * as codeGraph from '@/lib/code-graph';
import { useRepoGraphProgress } from '@/hooks/useRepoGraphProgress';

const Wrapper: FC<{ children: ReactNode }> = ({ children }) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
};

describe('useRepoGraphProgress', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('polls every 5s while incomplete', async () => {
    vi.spyOn(codeGraph, 'getRepoGraphProgress').mockResolvedValue({
      is_complete: false,
      processed: 5,
      total: 20,
      last_file: 'src/foo.ts',
      status: 'running',
    });
    const { result } = renderHook(() => useRepoGraphProgress(7), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.data?.processed).toBe(5));
    expect(result.current.refetchInterval).toBe(5000);
  });

  it('polls every 60s when complete', async () => {
    vi.spyOn(codeGraph, 'getRepoGraphProgress').mockResolvedValue({
      is_complete: true,
      processed: 20,
      total: 20,
      last_file: 'src/zzz.ts',
      status: 'unchanged',
    });
    const { result } = renderHook(() => useRepoGraphProgress(7), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.data?.is_complete).toBe(true));
    expect(result.current.refetchInterval).toBe(60000);
  });
});
