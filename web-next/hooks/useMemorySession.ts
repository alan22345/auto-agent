'use client';
import { useState, useCallback } from 'react';
import { useWS } from './useWS';
import type { MemoryRow } from '@/types/ws';

export type MemoryStatus = { text: string; isError?: boolean };

export function useMemorySession() {
  const [rows, setRows] = useState<MemoryRow[]>([]);
  const [sourceId, setSourceId] = useState<string | null>(null);
  const [status, setStatus] = useState<MemoryStatus>({ text: '' });
  const [results, setResults] = useState<{ ok: boolean; error?: string }[] | null>(null);

  useWS('memory_rows', (e) => {
    setRows(e.rows);
    if (e.source_id) setSourceId(e.source_id);
    setStatus({ text: '' });
    setResults(null);
  });
  useWS('memory_saved', (e) => {
    setResults(e.results);
    const allOk = e.results.every((r) => r.ok);
    setStatus({ text: allOk ? 'All saved.' : 'Some rows failed — see statuses.', isError: !allOk });
    if (allOk) {
      setRows([]);
      setSourceId(null);
    }
  });
  useWS('memory_error', (e) => setStatus({ text: e.message, isError: true }));

  const updateRow = useCallback((i: number, patch: Partial<MemoryRow>) => {
    setRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }, []);

  const addRow = useCallback(() => {
    setRows((prev) => [
      ...prev,
      {
        row_id: 'r-' + Math.random().toString(36).slice(2, 10),
        entity: '',
        entity_type: 'concept',
        entity_status: 'new',
        kind: 'fact',
        content: '',
        conflicts: [],
        resolution: null,
      },
    ]);
  }, []);

  const deleteRow = useCallback((i: number) => {
    setRows((prev) => prev.filter((_, idx) => idx !== i));
  }, []);

  const reset = useCallback(() => {
    setRows([]);
    setSourceId(null);
    setStatus({ text: '' });
    setResults(null);
  }, []);

  return { rows, sourceId, setSourceId, status, setStatus, results, updateRow, addRow, deleteRow, reset };
}
