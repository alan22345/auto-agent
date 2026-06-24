'use client';
import { useMemo, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, RefreshCw, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { BranchPicker } from '@/components/code-graph/branch-picker';
import { RefreshButton } from '@/components/code-graph/refresh-button';
import { FreshnessBanner } from '@/components/code-graph/freshness-banner';
import { EdgeEvidencePopover } from '@/components/code-graph/edge-evidence-popover';
import { AreaFilter } from '@/components/code-graph/area-filter';
import { EdgeKindFilter } from '@/components/code-graph/edge-kind-filter';
import { GraphCanvas } from '@/components/code-graph/graph-canvas';
import { NodeSidePanel } from '@/components/code-graph/node-side-panel';
import { SearchInput } from '@/components/code-graph/search-input';
import { ViolationsPanel } from '@/components/code-graph/violations-panel';
import {
  MapCanvas,
  type FocusPath,
} from '@/components/code-graph/map-canvas';
import {
  encodeFocusForQuery,
  parseFocusFromQuery,
} from '@/lib/code-graph-focus';
import { MapEmpty } from '@/components/code-graph/map-tiles';
import type { Edge } from '@/types/api';
import { ApiError } from '@/lib/api';
import { disableRepoGraph } from '@/lib/code-graph';
import { codeGraphKeys } from '@/hooks/useCodeGraphConfigs';
import { useCodeGraphConfig } from '@/hooks/useCodeGraphConfig';
import { useRepoGraph } from '@/hooks/useRepoGraph';
import { useRepoGraphStaleness } from '@/hooks/useRepoGraphStaleness';
import { useRepoGraphProgress } from '@/hooks/useRepoGraphProgress';
import {
  useRecomputeRepoGraphFlows,
  useRepoGraphFlows,
} from '@/hooks/useRepoGraphFlows';
import { GraphCompletionBadge } from '@/components/code-graph/graph-completion-badge';
import { HealthTab } from '@/components/code-graph/health-tab';

type TabKey = 'map' | 'raw' | 'health';

// ADR-016 §11 — per-repo settings + graph page.
//
// Phase 3 of the capability-flow map spec adds the Map/Raw tab bar.
// ``Map`` is the default. Tab + focus path are encoded in the URL so
// deep links and browser back/forward work (Phase 5 §6).
export default function CodeGraphRepoPage({
  params,
}: {
  params: { repoId: string };
}) {
  const repoId = Number(params.repoId);
  const router = useRouter();
  const searchParams = useSearchParams();
  const qc = useQueryClient();
  const [disableError, setDisableError] = useState<string | null>(null);
  const [highlightedEdgeId, setHighlightedEdgeId] = useState<string | null>(
    null,
  );
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<{
    id: string;
    pos: { x: number; y: number };
  } | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [hiddenEdgeKinds, setHiddenEdgeKinds] = useState<Set<Edge['kind']>>(
    () => new Set(),
  );
  // 2026-05-21 — bulk per-area hide. Empty = all areas visible.
  const [hiddenAreas, setHiddenAreas] = useState<Set<string>>(() => new Set());
  // Phase 7 P2c §11 — ancestor / descendant highlight subgraph. The
  // side panel computes it; the canvas paints it. Clears whenever the
  // selected node changes so a stale highlight doesn't sit around.
  const [reachabilityHighlight, setReachabilityHighlight] = useState<
    Set<string> | null
  >(null);

  // Phase 3+ — Map tab state, URL-backed (Phase 5 §6). Tab key + focus
  // path live in the URL so reload, back/forward, and deep-link sharing
  // all do the right thing.
  const tabFromUrl = (searchParams.get('tab') ?? 'map').toLowerCase();
  const activeTab: TabKey =
    tabFromUrl === 'raw'
      ? 'raw'
      : tabFromUrl === 'health'
        ? 'health'
        : 'map';
  const focusPath = parseFocusFromQuery(searchParams.get('p'));

  const setTab = (tab: TabKey) => {
    const next = new URLSearchParams(searchParams.toString());
    next.set('tab', tab);
    router.replace(`?${next.toString()}`, { scroll: false });
  };
  const setFocus = (focus: FocusPath) => {
    const next = new URLSearchParams(searchParams.toString());
    const encoded = encodeFocusForQuery(focus);
    if (encoded) next.set('p', encoded);
    else next.delete('p');
    router.replace(`?${next.toString()}`, { scroll: false });
  };

  // Phase 5 §6 keyboard nav (Esc / ArrowUp drill out, Home → LOD 0)
  // lives on MapCanvas itself, so any host of the component gets the
  // bindings for free. See ``components/code-graph/map-canvas.tsx``.

  const { data: config, isLoading, isError, error } = useCodeGraphConfig(
    Number.isFinite(repoId) ? repoId : null,
  );
  const { data: latest } = useRepoGraph(Number.isFinite(repoId) ? repoId : null);
  const { data: staleness } = useRepoGraphStaleness(
    Number.isFinite(repoId) ? repoId : null,
    Boolean(latest?.blob),
  );
  const progressQuery = useRepoGraphProgress(Number.isFinite(repoId) ? repoId : null);
  const progress = progressQuery.data;
  const { data: flows } = useRepoGraphFlows(
    Number.isFinite(repoId) ? repoId : null,
  );
  const recomputeFlowsMutation = useRecomputeRepoGraphFlows(
    Number.isFinite(repoId) ? repoId : null,
  );
  const computeError = useMemo(() => {
    const err = recomputeFlowsMutation.error;
    if (!err) return null;
    if (err instanceof ApiError) return err.detail;
    if (err instanceof Error) return err.message;
    return 'Recompute failed.';
  }, [recomputeFlowsMutation.error]);

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
            <div className="flex items-center gap-4">
              {latest && <FreshnessBanner latest={latest} staleness={staleness} />}
              {progress ? <GraphCompletionBadge progress={progress} /> : null}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <RefreshButton repoId={config.repo_id} isComplete={progress?.is_complete ?? true} />
              <span className="ml-auto" />
              <BranchPicker config={config} />
            </div>

            <Tabs
              value={activeTab}
              onValueChange={(v) => setTab(v as TabKey)}
              className="flex min-h-0 flex-1 flex-col"
            >
              <TabsList className="self-start">
                <TabsTrigger value="map">Map</TabsTrigger>
                <TabsTrigger value="raw">Raw graph</TabsTrigger>
                <TabsTrigger value="health">Health</TabsTrigger>
              </TabsList>

              <TabsContent value="map" className="min-h-0 flex-1">
                {flows?.blob ? (
                  <div className="flex h-full flex-col">
                    <div className="flex flex-wrap items-center gap-3 border-y bg-card/40 px-2 py-2">
                      <SearchInput onChange={setSearchQuery} />
                      <span className="ml-auto" />
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => recomputeFlowsMutation.mutate()}
                        disabled={recomputeFlowsMutation.isPending}
                      >
                        <RefreshCw size={12} className="mr-1.5" />
                        {recomputeFlowsMutation.isPending
                          ? 'Recomputing…'
                          : 'Recompute map'}
                      </Button>
                    </div>
                    {computeError && (
                      <p role="alert" className="px-2 py-1 text-xs text-destructive">
                        {computeError}
                      </p>
                    )}
                    <div className="min-h-0 flex-1 overflow-hidden">
                      <MapCanvas
                        blob={flows.blob}
                        focus={focusPath}
                        onFocusChange={setFocus}
                        searchQuery={searchQuery}
                        repoId={config.repo_id}
                        graphBlob={latest?.blob ?? null}
                      />
                    </div>
                  </div>
                ) : (
                  <MapEmpty
                    hasFlows={false}
                    onCompute={() => recomputeFlowsMutation.mutate()}
                    computing={recomputeFlowsMutation.isPending}
                    computeError={computeError}
                  />
                )}
              </TabsContent>

              <TabsContent value="raw" className="min-h-0 flex-1">
                {latest?.blob ? (
                  <>
                    <div
                      className="flex flex-wrap items-center gap-3 border-y bg-card/40 px-2 py-2"
                      data-testid="graph-toolbar"
                    >
                      <SearchInput onChange={setSearchQuery} />
                      <EdgeKindFilter onChange={setHiddenEdgeKinds} />
                      <AreaFilter
                        areas={latest.blob.areas.map((a) => a.name)}
                        onChange={setHiddenAreas}
                      />
                    </div>
                    <div className="flex min-h-0 flex-1 gap-4">
                      <div className="min-w-0 flex-1">
                        <GraphCanvas
                          blob={latest.blob}
                          highlightedEdgeId={highlightedEdgeId}
                          repoId={config.repo_id}
                          onNodeClick={(id) => {
                            setReachabilityHighlight(null);
                            setSelectedNodeId(id);
                          }}
                          onEdgeClick={(id, pos) =>
                            setSelectedEdge({ id, pos })
                          }
                          searchQuery={searchQuery}
                          hiddenEdgeKinds={hiddenEdgeKinds}
                          hiddenAreas={hiddenAreas}
                          reachabilityHighlight={reachabilityHighlight}
                        />
                      </div>
                      {selectedNodeId && (
                        <NodeSidePanel
                          repoId={config.repo_id}
                          blob={latest.blob}
                          nodeId={selectedNodeId}
                          onSelectEdge={setHighlightedEdgeId}
                          onShowEdgeEvidence={(id, pos) =>
                            setSelectedEdge({ id, pos })
                          }
                          onHighlightReachability={setReachabilityHighlight}
                          onClose={() => {
                            setReachabilityHighlight(null);
                            setSelectedNodeId(null);
                          }}
                        />
                      )}
                    </div>
                    {selectedEdge && (
                      <EdgeEvidencePopover
                        blob={latest.blob}
                        edgeId={selectedEdge.id}
                        position={selectedEdge.pos}
                        onClose={() => setSelectedEdge(null)}
                      />
                    )}
                    <ViolationsPanel
                      blob={latest.blob}
                      highlightedEdgeId={highlightedEdgeId}
                      onSelectEdge={setHighlightedEdgeId}
                    />
                  </>
                ) : (
                  <div
                    role="status"
                    className="flex h-[400px] items-center justify-center rounded-md border bg-card/40 text-sm text-muted-foreground"
                  >
                    Analysis in progress — first analysis can take a few minutes.
                  </div>
                )}
              </TabsContent>

              <TabsContent value="health" className="min-h-0 flex-1">
                {latest?.blob ? (
                  <HealthTab blob={latest.blob} repoId={repoId} />
                ) : (
                  <div
                    role="status"
                    className="flex h-[400px] items-center justify-center rounded-md border bg-card/40 text-sm text-muted-foreground"
                  >
                    Analysis in progress — first analysis can take a few
                    minutes.
                  </div>
                )}
              </TabsContent>
            </Tabs>

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

// URL <-> focus path helpers live in ``lib/code-graph-focus.ts`` —
// Next.js does not allow arbitrary named exports from a page file, and
// the production build's typecheck enforces it.
