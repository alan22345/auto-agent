'use client';
// v1 stand-in: ADRs live in the workspace at docs/decisions/ and are not yet
// served by the orchestrator. We surface a count of architect attempts that
// produced a commit (a proxy for "wrote work / ADRs") so the user has a hook
// from the task UI back to the underlying git history. A future enhancement
// will add an ADR-listing endpoint and render the file titles inline.
import { useArchitectAttempts } from '@/hooks/useTrioArtifacts';

export function DecisionsPanel({ taskId }: { taskId: number }) {
  const { data = [], isLoading } = useArchitectAttempts(taskId);
  if (isLoading) {
    return <div className="text-xs text-muted-foreground">Loading…</div>;
  }
  const commits = data.filter((a) => a.commit_sha);
  return (
    <div className="rounded border bg-muted/30 px-3 py-2 text-xs">
      <div className="font-medium">Decisions</div>
      <div className="mt-1 text-muted-foreground">
        Architect ADRs live in <code>docs/decisions/</code> on the task branch.
      </div>
      <div className="mt-1">
        {commits.length} architect commit{commits.length === 1 ? '' : 's'} so far
        {commits.length > 0 && (
          <span className="text-muted-foreground">
            {' '}
            (latest{' '}
            <code className="text-[10px]">
              {commits[commits.length - 1].commit_sha!.slice(0, 7)}
            </code>
            )
          </span>
        )}
      </div>
    </div>
  );
}
