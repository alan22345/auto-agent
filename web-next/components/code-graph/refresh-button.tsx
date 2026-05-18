'use client';
import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { refreshRepoGraph } from '@/lib/code-graph';
import { ApiError } from '@/lib/api';
import { codeGraphKeys } from '@/hooks/useCodeGraphConfigs';

interface Props {
  repoId: number;
}

// Renders the "Refresh whole graph" control from ADR-016 §11.
//
// Phase 2: the endpoint returns 202 with a request_id; the handler in
// the agent process clones / fetches the workspace, runs the analyser,
// writes a ``RepoGraph`` row, then publishes ``REPO_GRAPH_READY``. We
// invalidate the cached "latest" query so the page picks up the new
// row when the user-visible polling-or-WS arrival fires the next fetch.
export function RefreshButton({ repoId }: Props) {
  const [feedback, setFeedback] = useState<string | null>(null);
  const qc = useQueryClient();

  const mutation = useMutation({
    mutationFn: () => refreshRepoGraph(repoId),
    onSuccess: (resp) => {
      setFeedback(`Refresh queued (request ${resp.request_id.slice(0, 8)}).`);
      // Drop the cached blob; the polling hook will refetch.
      qc.invalidateQueries({
        queryKey: [...codeGraphKeys.config(repoId), 'latest'],
      });
    },
    onError: (err: unknown) => {
      if (err instanceof ApiError) {
        setFeedback(err.detail);
      } else if (err instanceof Error) {
        setFeedback(err.message);
      } else {
        setFeedback('Refresh failed.');
      }
    },
  });

  return (
    <div className="flex flex-col gap-2">
      <Button onClick={() => mutation.mutate()} disabled={mutation.isPending} variant="secondary">
        <RefreshCw size={14} className="mr-2" />
        {mutation.isPending ? 'Refreshing…' : 'Refresh whole graph'}
      </Button>
      {feedback && (
        <p
          role={mutation.isError ? 'alert' : 'status'}
          className="text-xs text-muted-foreground max-w-md"
        >
          {feedback}
        </p>
      )}
    </div>
  );
}
