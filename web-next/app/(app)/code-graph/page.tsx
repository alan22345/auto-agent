'use client';
import { useState } from 'react';
import Link from 'next/link';
import { Network, Plus } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { OnboardModal } from '@/components/code-graph/onboard-modal';
import { useCodeGraphConfigs } from '@/hooks/useCodeGraphConfigs';

// ADR-016 §11 — top-level /code-graph route. Lists every repo that has
// graph analysis enabled for the caller's org; empty state explains how
// to onboard the first one.
export default function CodeGraphPage() {
  const { data, isLoading, isError, error } = useCodeGraphConfigs();
  const [modalOpen, setModalOpen] = useState(false);
  const configs = data ?? [];
  const enabledIds = configs.map((c) => c.repo_id);

  return (
    <div className="flex h-full flex-col overflow-hidden p-6">
      <header className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Code graph</h1>
          <p className="text-sm text-muted-foreground">
            Function-level structural map of an onboarded repo. Both the agent and you read
            the same graph (ADR-016).
          </p>
        </div>
        <Button onClick={() => setModalOpen(true)}>
          <Plus size={14} className="mr-2" />
          Enable for a repo
        </Button>
      </header>

      <section className="min-h-0 flex-1 overflow-auto">
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : isError ? (
          <p role="alert" className="text-sm text-destructive">
            Failed to load: {error instanceof Error ? error.message : 'unknown error'}
          </p>
        ) : configs.length === 0 ? (
          <EmptyState onCTA={() => setModalOpen(true)} />
        ) : (
          <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {configs.map((cfg) => (
              <li key={cfg.repo_id}>
                <Link
                  href={`/code-graph/${cfg.repo_id}`}
                  className="block rounded-lg border bg-card/40 p-4 hover:bg-card/80"
                >
                  <div className="flex items-center gap-2 font-medium">
                    <Network size={16} className="shrink-0 text-muted-foreground" />
                    <span className="truncate">{cfg.repo_name}</span>
                  </div>
                  <p className="mt-2 text-xs text-muted-foreground">
                    Branch: <span className="font-mono">{cfg.analysis_branch}</span>
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {cfg.last_analysis_id == null
                      ? 'Not analysed yet'
                      : `Last analysed analysis #${cfg.last_analysis_id}`}
                  </p>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      <OnboardModal
        alreadyEnabledRepoIds={enabledIds}
        open={modalOpen}
        onOpenChange={setModalOpen}
      />
    </div>
  );
}

function EmptyState({ onCTA }: { onCTA: () => void }) {
  return (
    <div
      role="region"
      aria-label="No graph-enabled repos"
      className="mx-auto mt-12 max-w-md rounded-lg border bg-card/30 p-8 text-center"
    >
      <Network size={32} className="mx-auto text-muted-foreground" />
      <h2 className="mt-4 font-semibold">No repos are graph-enabled yet.</h2>
      <p className="mt-2 text-sm text-muted-foreground">
        Turn on graph analysis for a repo and the agent (and you) will be able to navigate its
        structure visually. The first analysis lands in Phase 2 of ADR-016 — for now you can
        configure which repos are eligible and on which branch.
      </p>
      <Button className="mt-4" onClick={onCTA}>
        <Plus size={14} className="mr-2" />
        Enable for a repo
      </Button>
    </div>
  );
}
