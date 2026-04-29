'use client';
import { useState } from 'react';
import { useFreeformConfigs } from '@/hooks/useFreeformConfigs';
import { FreeformSidebar } from '@/components/freeform/sidebar';
import { BuildView } from '@/components/freeform/build-view';
import { ExistingView } from '@/components/freeform/existing-view';
import { RepoDetail } from '@/components/freeform/repo-detail';
import type { FreeformView } from '@/components/freeform/sidebar';
import type { FreeformConfig } from '@/types/ws';

export default function FreeformPage() {
  const configs = useFreeformConfigs();
  const [view, setView] = useState<FreeformView>(null);
  // Allow sidebar to optimistically remove a deleted config
  const [localConfigs, setLocalConfigs] = useState<FreeformConfig[] | null>(null);

  const effectiveConfigs = localConfigs ?? configs;

  return (
    <div className="flex h-full">
      <FreeformSidebar
        configs={effectiveConfigs}
        view={view}
        onView={setView}
        onConfigsChange={setLocalConfigs}
      />
      <section className="flex-1 overflow-auto p-6">
        {view?.kind === 'build' && <BuildView />}
        {view?.kind === 'existing' && <ExistingView />}
        {view?.kind === 'repo' && (
          <RepoDetail
            config={effectiveConfigs.find((c) => c.repo_name === view.name) ?? null}
          />
        )}
        {!view && (
          <p className="text-muted-foreground text-sm">
            Select an option from the sidebar to get started.
          </p>
        )}
      </section>
    </div>
  );
}
