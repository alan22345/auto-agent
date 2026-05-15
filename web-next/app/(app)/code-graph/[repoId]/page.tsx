'use client';
import { use, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { BranchPicker } from '@/components/code-graph/branch-picker';
import { RefreshButton } from '@/components/code-graph/refresh-button';
import { ApiError } from '@/lib/api';
import { disableRepoGraph } from '@/lib/code-graph';
import { codeGraphKeys } from '@/hooks/useCodeGraphConfigs';
import { useCodeGraphConfig } from '@/hooks/useCodeGraphConfig';

// ADR-016 §11 — per-repo settings page. The graph rendering itself
// (Cytoscape, expand/collapse) lands in Phase 2/7; Phase 1 ships
// configuration + the refresh trigger.
export default function CodeGraphRepoPage(props: { params: Promise<{ repoId: string }> }) {
  const params = use(props.params);
  const repoId = Number(params.repoId);
  const router = useRouter();
  const qc = useQueryClient();
  const [disableError, setDisableError] = useState<string | null>(null);

  const { data: config, isLoading, isError, error } = useCodeGraphConfig(
    Number.isFinite(repoId) ? repoId : null,
  );

  const disableMutation = useMutation({
    mutationFn: () => disableRepoGraph(repoId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: codeGraphKeys.configs });
      router.push('/code-graph');
    },
    onError: (err: unknown) => {
      if (err instanceof ApiError) setDisableError(err.detail);
      else if (err instanceof Error) setDisableError(err.message);
      else setDisableError('Failed to disable graph.');
    },
  });

  if (!Number.isFinite(repoId)) {
    return (
      <div className="p-6">
        <p role="alert" className="text-sm text-destructive">
          Invalid repo id.
        </p>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-hidden p-6">
      <header className="mb-6 flex items-center justify-between gap-4">
        <div>
          <Link
            href="/code-graph"
            className="mb-2 inline-flex items-center text-xs text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft size={14} className="mr-1" /> Back to graphs
          </Link>
          <h1 className="text-xl font-semibold">
            {config?.repo_name ?? `Repo ${repoId}`}
          </h1>
          {config && (
            <p className="text-xs text-muted-foreground">
              <span className="font-mono">{config.repo_url}</span>
            </p>
          )}
        </div>
      </header>

      <section className="min-h-0 flex-1 space-y-8 overflow-auto">
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : isError ? (
          <p role="alert" className="text-sm text-destructive">
            {error instanceof Error ? error.message : 'Failed to load config.'}
          </p>
        ) : !config ? (
          <p className="text-sm text-muted-foreground">
            Graph analysis is not enabled for this repo.{' '}
            <Link href="/code-graph" className="underline">
              Pick another repo
            </Link>{' '}
            or enable it from the list page.
          </p>
        ) : (
          <>
            <FreshnessBanner config={config} />

            <div className="space-y-4">
              <h2 className="text-sm font-semibold">Branch</h2>
              <BranchPicker config={config} />
            </div>

            <div className="space-y-4">
              <h2 className="text-sm font-semibold">Refresh</h2>
              <RefreshButton repoId={config.repo_id} />
            </div>

            <div className="space-y-4">
              <h2 className="text-sm font-semibold text-destructive">Danger zone</h2>
              <p className="text-xs text-muted-foreground max-w-md">
                Disabling graph analysis removes the config row. The on-disk workspace is
                left as-is and re-cloned the next time you enable the graph.
              </p>
              <Button
                variant="destructive"
                onClick={() => disableMutation.mutate()}
                disabled={disableMutation.isPending}
              >
                <Trash2 size={14} className="mr-2" />
                {disableMutation.isPending ? 'Disabling…' : 'Disable graph analysis'}
              </Button>
              {disableError && (
                <p role="alert" className="text-xs text-destructive">
                  {disableError}
                </p>
              )}
            </div>
          </>
        )}
      </section>
    </div>
  );
}

function FreshnessBanner({
  config,
}: {
  config: {
    analysis_branch: string;
    last_analysis_id?: number | null;
    analyser_version?: string;
    updated_at?: string | null;
  };
}) {
  const text =
    config.last_analysis_id == null
      ? `No analysis yet on branch ${config.analysis_branch}.`
      : `Graph from ${config.analysis_branch}, analysis #${config.last_analysis_id} (analyser ${config.analyser_version || 'unknown'}).`;
  return (
    <div className="rounded-md border bg-card/40 p-3 text-xs text-muted-foreground">
      {text}
    </div>
  );
}
