import { useQuery } from '@tanstack/react-query';
import { getRepoGraphProgress } from '@/lib/code-graph';
import type { RepoGraphProgressData } from '@/types/api';

const SHORT_INTERVAL = 5_000;
const LONG_INTERVAL = 60_000;

/**
 * Polls /repos/{repoId}/graph/progress.
 *
 * - 5s cadence while is_complete=false (the analyser is running).
 * - 60s cadence once is_complete=true (mostly to detect a fresh run).
 *
 * Returns the TanStack query result plus the chosen `refetchInterval`
 * (exposed for testability — assertion in the unit test).
 */
export function useRepoGraphProgress(repoId: number) {
  const query = useQuery<RepoGraphProgressData>({
    queryKey: ['repo-graph-progress', repoId],
    queryFn: () => getRepoGraphProgress(repoId),
    refetchInterval: (q) => {
      const data = q.state.data as RepoGraphProgressData | undefined;
      return data?.is_complete ? LONG_INTERVAL : SHORT_INTERVAL;
    },
    staleTime: 0,
  });
  const refetchInterval = query.data?.is_complete ? LONG_INTERVAL : SHORT_INTERVAL;
  return { ...query, refetchInterval };
}
