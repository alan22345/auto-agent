'use client';
import type { MemoryRow } from '@/types/ws';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { ConflictResolver } from './conflict-resolver';
import { wsClient } from '@/lib/ws';
import { cn } from '@/lib/utils';

const ENTITY_TYPES = ['project', 'concept', 'person', 'repo', 'system'] as const;
const KINDS = ['decision', 'architecture', 'gotcha', 'status', 'preference', 'fact'] as const;

interface Props {
  rows: MemoryRow[];
  sourceId: string | null;
  results: { ok: boolean; error?: string }[] | null;
  updateRow: (i: number, patch: Partial<MemoryRow>) => void;
  addRow: () => void;
  deleteRow: (i: number) => void;
  reset: () => void;
}

export function ReviewTable({ rows, sourceId, results, updateRow, addRow, deleteRow, reset }: Props) {
  const unresolved = rows.filter((r) => r.conflicts.length > 0 && !r.resolution).length;
  const saveLabel =
    unresolved > 0
      ? `Save all (${unresolved} conflict${unresolved > 1 ? 's' : ''} need review)`
      : 'Save all';

  if (rows.length === 0) return null;

  return (
    <div className="mt-4">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-base font-semibold">Proposed facts ({rows.length})</h3>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => {
            if (!sourceId) return;
            const note = prompt('Correction note for the agent:');
            if (note) wsClient.send({ type: 'memory_reextract', source_id: sourceId, note });
          }}
        >
          Re-extract with note…
        </Button>
      </div>
      <table className="w-full border-collapse rounded border text-xs">
        <thead className="bg-muted text-muted-foreground">
          <tr>
            <th className="p-2 text-left">Entity</th>
            <th className="p-2 text-left">Type</th>
            <th className="p-2 text-left">Kind</th>
            <th className="p-2 text-left">Content</th>
            <th className="p-2"></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr
              key={row.row_id}
              className={cn('border-t align-top', row.conflicts.length > 0 && 'bg-destructive/10')}
            >
              <td className="p-2">
                <Input
                  value={row.entity}
                  onChange={(e) => updateRow(i, { entity: e.target.value })}
                />
                <Badge variant="secondary" className="mt-1">
                  {row.entity_status}
                </Badge>
              </td>
              <td className="p-2">
                <select
                  className="rounded border bg-background p-1 text-xs"
                  value={row.entity_type}
                  onChange={(e) =>
                    updateRow(i, { entity_type: e.target.value as MemoryRow['entity_type'] })
                  }
                >
                  {ENTITY_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </td>
              <td className="p-2">
                <select
                  className="rounded border bg-background p-1 text-xs"
                  value={row.kind}
                  onChange={(e) =>
                    updateRow(i, { kind: e.target.value as MemoryRow['kind'] })
                  }
                >
                  {KINDS.map((k) => (
                    <option key={k} value={k}>
                      {k}
                    </option>
                  ))}
                </select>
              </td>
              <td className="p-2">
                <Textarea
                  rows={2}
                  value={row.content}
                  onChange={(e) => updateRow(i, { content: e.target.value })}
                />
                {row.conflicts.length > 0 && (
                  <ConflictResolver row={row} index={i} updateRow={updateRow} />
                )}
                {results?.[i] && (
                  <span
                    className={cn('text-xs', results[i].ok ? 'text-success' : 'text-destructive')}
                  >
                    {results[i].ok ? '✓ saved' : `✕ ${results[i].error || 'failed'}`}
                  </span>
                )}
              </td>
              <td className="p-2">
                <Button variant="ghost" size="sm" onClick={() => deleteRow(i)}>
                  ×
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="mt-2">
        <Button variant="secondary" size="sm" onClick={addRow}>
          + Add row
        </Button>
      </div>
      <div className="mt-3 flex justify-end gap-2">
        <Button variant="secondary" onClick={reset}>
          Discard
        </Button>
        <Button
          disabled={unresolved > 0}
          onClick={() => wsClient.send({ type: 'memory_save', rows, source_id: sourceId })}
        >
          {saveLabel}
        </Button>
      </div>
    </div>
  );
}
