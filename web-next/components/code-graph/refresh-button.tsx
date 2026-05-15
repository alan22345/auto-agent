'use client';
import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { refreshRepoGraph } from '@/lib/code-graph';
import { ApiError } from '@/lib/api';

interface Props {
  repoId: number;
}

// Renders the "Refresh whole graph" control from ADR-016 §11. Phase 1 of
// ADR-016 only ships configuration scaffolding — the underlying endpoint
// returns 501 until Phase 2's analyser lands. The 501 response carries a
// human-readable message that this component surfaces verbatim so users
// know exactly why nothing happens.
export function RefreshButton({ repoId }: Props) {
  const [feedback, setFeedback] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () => refreshRepoGraph(repoId),
    onSuccess: () => {
      setFeedback('Refresh queued.');
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
