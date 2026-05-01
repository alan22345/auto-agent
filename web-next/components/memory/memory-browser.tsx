'use client';
import { useMemoryBrowser } from '@/hooks/useMemoryBrowser';
import { EntityDetail } from './entity-detail';
import { EntityList } from './entity-list';
import { EntitySearch } from './entity-search';

export function MemoryBrowser() {
  const b = useMemoryBrowser();
  const inSearchMode = b.query.trim().length >= 2;
  const list = inSearchMode ? b.results : b.recent;
  const isLoading = list === null;

  return (
    <div className="flex h-full flex-col gap-3">
      <div>
        <h2 className="mb-2 text-sm font-semibold">Memory</h2>
        <EntitySearch value={b.query} onChange={b.setQuery} />
      </div>
      {b.globalError && (
        <p className="rounded border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive">
          {b.globalError}
        </p>
      )}
      <div className="flex-1 overflow-auto">
        {b.selected ? (
          <EntityDetail
            detail={b.selected}
            includeSuperseded={b.includeSuperseded}
            setIncludeSuperseded={b.setIncludeSuperseded}
            pendingByFact={b.pendingByFact}
            factErrors={b.factErrors}
            onBack={b.clearSelection}
            onEdit={b.editFact}
            onCorrect={b.correctFact}
            onDelete={b.deleteFact}
          />
        ) : isLoading ? (
          <p className="px-2 py-6 text-center text-xs text-muted-foreground">Loading…</p>
        ) : (
          <>
            {!inSearchMode && (
              <p className="mb-2 text-xs text-muted-foreground">Recent activity</p>
            )}
            <EntityList entities={list} onSelect={b.selectEntity} />
          </>
        )}
      </div>
    </div>
  );
}
