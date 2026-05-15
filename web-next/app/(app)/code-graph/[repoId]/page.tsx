'use client';
import { use, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { BranchPicker } from '@/components/code-graph/branch-picker';
import { RefreshButton } from '@/components/code-graph/refresh-button';
import { FreshnessBanner } from '@/components/code-graph/freshness-banner';
import { GraphCanvas } from '@/components/code-graph/graph-canvas';
import { ApiError } from '@/lib/api';
import { disableRepoGraph } from '@/lib/code-graph';
import { codeGraphKeys } from '@/hooks/useCodeGraphConfigs';
import { useCodeGraphConfig } from '@/hooks/useCodeGraphConfig';
import { useRepoGraph } from '@/hooks/useRepoGraph';

// ADR-016 §11 — per-repo settings + graph page. Phase 2 wires the
// Cytoscape canvas in below the freshness banner; Phase 7 polishes
// node interactions + side-panel evidence.
export default function CodeGraphRepoPage(props: { params: Promise<{ repoId: string }> }) {
  const params = use(props.params);
  const repoId = Number(params.repoId);
  const router = useRouter();
  const qc = useQueryClient();
  const [disableError, setDisableError] = useState<string | null>(null);

  const { data: config, isLoading, isError, error } = useCodeGraphConfig(
    Number.isFinite(repoId) ? repoId : null,
  );
  const { data: latest } = useRepoGraph(Number.isFinite(repoId) ? repoId : null);

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

      <section className="min-h-0 flex-1 space-y-6 overflow-auto">
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
            {latest && <FreshnessBanner latest={latest} />}

            <div className="flex flex-wrap items-center gap-2">
              <RefreshButton repoId={config.repo_id} />
              <span className="ml-auto" />
              <BranchPicker config={config} />
            </div>

            {latest?.blob ? (
              <GraphCanvas blob={latest.blob} />
            ) : (
              <div
                role="status"
                className="flex h-[400px] items-center justify-center rounded-md border bg-card/40 text-sm text-muted-foreground"
              >
                Analysis in progress — first analysis can take a few minutes.
              </div>
            )}

            <div className="space-y-2 border-t pt-4">
              <h2 className="text-sm font-semibold text-destructive">Danger zone</h2>
              <p className="text-xs text-muted-foreground max-w-md">
                Disabling graph analysis removes the config row. The on-disk workspace
                is left as-is and re-cloned the next time you enable the graph.
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
