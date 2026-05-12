'use client';
import { useQuery } from '@tanstack/react-query';

import { fetchUsageSummary, type UsageSummary } from '@/lib/usage';

export function useUsageSummary() {
  return useQuery<UsageSummary>({
    queryKey: ['usage', 'summary'],
    queryFn: fetchUsageSummary,
    refetchInterval: 60_000,
  });
}
