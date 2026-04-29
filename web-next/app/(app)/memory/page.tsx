'use client';
import { useMemorySession } from '@/hooks/useMemorySession';
import { DropZone } from '@/components/memory/drop-zone';
import { ReviewTable } from '@/components/memory/review-table';
import { cn } from '@/lib/utils';

export default function MemoryPage() {
  const { rows, sourceId, setSourceId, status, setStatus, results, updateRow, addRow, deleteRow, reset } =
    useMemorySession();

  return (
    <div className="h-full overflow-auto p-6">
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
    </div>
  );
}
