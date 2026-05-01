'use client';
import { useMemorySession } from '@/hooks/useMemorySession';
import { DropZone } from '@/components/memory/drop-zone';
import { ReviewTable } from '@/components/memory/review-table';
import { MemoryBrowser } from '@/components/memory/memory-browser';
import { cn } from '@/lib/utils';

export default function MemoryPage() {
  const { rows, sourceId, setSourceId, status, setStatus, results, updateRow, addRow, deleteRow, reset } =
    useMemorySession();

  return (
    <div className="grid h-full grid-cols-1 gap-4 overflow-hidden p-6 lg:grid-cols-[360px_minmax(0,1fr)]">
      <aside className="min-h-0 overflow-hidden rounded-lg border bg-card/40 p-3">
        <MemoryBrowser />
      </aside>
      <section className="min-h-0 overflow-auto">
        <DropZone sourceId={sourceId} setSourceId={setSourceId} setStatus={setStatus} />
        {status.text && (
          <p className={cn('mt-2 text-xs', status.isError ? 'text-destructive' : 'text-muted-foreground')}>
            {status.text}
          </p>
        )}
        <ReviewTable
          rows={rows}
          sourceId={sourceId}
          results={results}
          updateRow={updateRow}
          addRow={addRow}
          deleteRow={deleteRow}
          reset={reset}
        />
      </section>
    </div>
  );
}
