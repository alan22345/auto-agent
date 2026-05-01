'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useWS } from './useWS';
import { wsClient } from '@/lib/ws';
import type { MemoryEntityDetail, MemoryEntitySummary, MemoryFact } from '@/types/api';

export type FactOp = 'edit' | 'correct' | 'delete';

export interface FactError {
  fact_id: string;
  message: string;
}

export interface UseMemoryBrowser {
  query: string;
  setQuery: (q: string) => void;
  results: MemoryEntitySummary[] | null;
  recent: MemoryEntitySummary[] | null;
  selected: MemoryEntityDetail | null;
  includeSuperseded: boolean;
  setIncludeSuperseded: (v: boolean) => void;
  pendingByFact: Record<string, FactOp>;
  factErrors: Record<string, string>;
  globalError: string | null;
  selectEntity: (name: string) => void;
  clearSelection: () => void;
  editFact: (fact_id: string, content: string) => void;
  correctFact: (fact_id: string, content: string, reason: string) => void;
  deleteFact: (fact_id: string) => void;
  refresh: () => void;
}

const SEARCH_MIN_CHARS = 2;
const DEBOUNCE_MS = 250;

export function useMemoryBrowser(): UseMemoryBrowser {
  const [query, setQueryState] = useState('');
  const [results, setResults] = useState<MemoryEntitySummary[] | null>(null);
  const [recent, setRecent] = useState<MemoryEntitySummary[] | null>(null);
  const [selected, setSelected] = useState<MemoryEntityDetail | null>(null);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [includeSuperseded, setIncludeSuperseded] = useState(false);
  const [pendingByFact, setPending] = useState<Record<string, FactOp>>({});
  const [factErrors, setFactErrors] = useState<Record<string, string>>({});
  const [globalError, setGlobalError] = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const sendSearch = useCallback((q: string) => {
    wsClient.send({ type: 'memory_search', query: q });
  }, []);

  const sendGetEntity = useCallback((name: string, withSuperseded: boolean) => {
    wsClient.send({
      type: 'memory_get_entity',
      entity: name,
      include_superseded: withSuperseded,
    });
  }, []);

  // Initial load: recent entities. wsClient.send returns false if the socket
  // isn't open yet — Providers' connect() runs in a parent useEffect that
  // fires AFTER ours on cold mount, so we retry until it goes through.
  useEffect(() => {
    if (wsClient.send({ type: 'memory_search', query: '' })) return;
    let attempts = 0;
    const id = setInterval(() => {
      if (wsClient.send({ type: 'memory_search', query: '' }) || ++attempts > 50) {
        clearInterval(id);
      }
    }, 100);
    return () => clearInterval(id);
  }, []);

  useWS('memory_search_results', (e) => {
    if (e.query) setResults(e.entities);
    else setRecent(e.entities);
  });

  useWS('memory_entity', (e) => {
    setSelected(e.detail);
    setSelectedName(e.detail.entity.name);
  });

  useWS('memory_fact_corrected', (e) => {
    setPending((prev) => {
      const next = { ...prev };
      delete next[e.fact_id];
      return next;
    });
    setFactErrors((prev) => {
      const next = { ...prev };
      delete next[e.fact_id];
      return next;
    });
    if (selectedName) sendGetEntity(selectedName, includeSuperseded);
    sendSearch('');
    if (query.length >= SEARCH_MIN_CHARS) sendSearch(query);
  });

  useWS('memory_fact_deleted', (e) => {
    setPending((prev) => {
      const next = { ...prev };
      delete next[e.fact_id];
      return next;
    });
    setFactErrors((prev) => {
      const next = { ...prev };
      delete next[e.fact_id];
      return next;
    });
    if (selectedName) sendGetEntity(selectedName, includeSuperseded);
    sendSearch('');
    if (query.length >= SEARCH_MIN_CHARS) sendSearch(query);
  });

  useWS('memory_error', (e) => {
    setGlobalError(e.message);
    // Roll back any optimistic state — the server doesn't tell us which fact
    // failed, so the safest move is to refetch the entity.
    setPending({});
    if (selectedName) sendGetEntity(selectedName, includeSuperseded);
  });

  const setQuery = useCallback(
    (q: string) => {
      setQueryState(q);
      setGlobalError(null);
      if (debounceRef.current) clearTimeout(debounceRef.current);
      const trimmed = q.trim();
      if (trimmed.length === 0) {
        setResults(null);
        return;
      }
      if (trimmed.length < SEARCH_MIN_CHARS) {
        setResults(null);
        return;
      }
      debounceRef.current = setTimeout(() => {
        sendSearch(trimmed);
      }, DEBOUNCE_MS);
    },
    [sendSearch],
  );

  const selectEntity = useCallback(
    (name: string) => {
      setGlobalError(null);
      setFactErrors({});
      setSelectedName(name);
      sendGetEntity(name, includeSuperseded);
    },
    [includeSuperseded, sendGetEntity],
  );

  const clearSelection = useCallback(() => {
    setSelected(null);
    setSelectedName(null);
    setFactErrors({});
  }, []);

  const setIncludeSupersededAndRefetch = useCallback(
    (v: boolean) => {
      setIncludeSuperseded(v);
      if (selectedName) sendGetEntity(selectedName, v);
    },
    [selectedName, sendGetEntity],
  );

  const applyOptimisticEdit = (fact_id: string, content: string) => {
    setSelected((prev) =>
      prev
        ? {
            ...prev,
            facts: (prev.facts ?? []).map((f: MemoryFact) =>
              f.id === fact_id ? { ...f, content } : f,
            ),
          }
        : prev,
    );
  };

  const editFact = useCallback((fact_id: string, content: string) => {
    setPending((p) => ({ ...p, [fact_id]: 'edit' }));
    setFactErrors((prev) => {
      const next = { ...prev };
      delete next[fact_id];
      return next;
    });
    applyOptimisticEdit(fact_id, content);
    wsClient.send({ type: 'memory_correct_fact', fact_id, content });
  }, []);

  const correctFact = useCallback((fact_id: string, content: string, reason: string) => {
    setPending((p) => ({ ...p, [fact_id]: 'correct' }));
    setFactErrors((prev) => {
      const next = { ...prev };
      delete next[fact_id];
      return next;
    });
    applyOptimisticEdit(fact_id, content);
    wsClient.send({ type: 'memory_correct_fact', fact_id, content, reason });
  }, []);

  const deleteFact = useCallback((fact_id: string) => {
    setPending((p) => ({ ...p, [fact_id]: 'delete' }));
    setFactErrors((prev) => {
      const next = { ...prev };
      delete next[fact_id];
      return next;
    });
    wsClient.send({ type: 'memory_delete_fact', fact_id });
  }, []);

  const refresh = useCallback(() => {
    sendSearch('');
    if (selectedName) sendGetEntity(selectedName, includeSuperseded);
  }, [includeSuperseded, selectedName, sendGetEntity, sendSearch]);

  return {
    query,
    setQuery,
    results,
    recent,
    selected,
    includeSuperseded,
    setIncludeSuperseded: setIncludeSupersededAndRefetch,
    pendingByFact,
    factErrors,
    globalError,
    selectEntity,
    clearSelection,
    editFact,
    correctFact,
    deleteFact,
    refresh,
  };
}
