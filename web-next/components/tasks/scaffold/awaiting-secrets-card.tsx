'use client';
import Link from 'next/link';
import { AlertTriangle, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useRepos } from '@/hooks/useRepos';
import { useRepoSecrets, useRecheckScaffoldSecrets } from '@/hooks/useRepoSecrets';

// ADR-019 §9 — banner shown on the scaffold task detail panel when the
// parent is parked at AWAITING_REQUIRED_SECRETS. Shows the count of
// unset architect-required keys and deep-links to the secrets page.

interface Props {
  taskId: number;
  repoName: string | null | undefined;
}

export function AwaitingSecretsCard({ taskId, repoName }: Props) {
  // Resolve repo_id from the repo name via the repos list.
  const { data: repos } = useRepos();
  const repoId = repos?.find((r) => r.name === repoName)?.id ?? null;

  const { data } = useRepoSecrets(repoId);
  const recheckMutation = useRecheckScaffoldSecrets(taskId);

  const missingCount =
    data?.keys.filter((k) => k.source === 'architect_required' && !k.is_set).length ?? 0;

  const secretsHref =
    repoId !== null
      ? `/repos/${repoId}/secrets?filter=architect_required`
      : undefined;

  return (
    <div
      role="alert"
      className="flex flex-col gap-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-4"
    >
      <div className="flex items-start gap-3">
        <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-600" aria-hidden />
        <div className="flex-1">
          <p className="text-sm font-medium text-amber-800 dark:text-amber-300">
            This build is waiting on{' '}
            <strong>
              {missingCount > 0 ? `${missingCount} required secret${missingCount !== 1 ? 's' : ''}` : 'required secrets'}
            </strong>
            . Set them to continue.
          </p>
          <p className="mt-1 text-xs text-amber-700/80 dark:text-amber-400/80">
            The build cannot dispatch domain teams until every architect-required secret is
            populated.
          </p>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {secretsHref && (
          <Link href={secretsHref}>
            <Button size="sm" variant="default">
              Set required secrets →
            </Button>
          </Link>
        )}
        <Button
          size="sm"
          variant="outline"
          onClick={() => recheckMutation.mutate()}
          disabled={recheckMutation.isPending}
        >
          <RefreshCw size={13} className={`mr-1 ${recheckMutation.isPending ? 'animate-spin' : ''}`} />
          {recheckMutation.isPending ? 'Checking…' : 'Recheck'}
        </Button>
        {recheckMutation.data && !recheckMutation.data.unblocked && recheckMutation.data.missing.length > 0 && (
          <span className="text-xs text-muted-foreground">
            Still missing: {recheckMutation.data.missing.join(', ')}
          </span>
        )}
        {recheckMutation.data?.unblocked && (
          <span className="text-xs text-green-600">Unblocked — build is now dispatching.</span>
        )}
      </div>
    </div>
  );
}
