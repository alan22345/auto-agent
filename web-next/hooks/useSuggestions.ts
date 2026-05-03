'use client';
import { useCallback, useEffect, useState } from 'react';
import { useWS } from './useWS';
import { wsClient } from '@/lib/ws';
import type { Suggestion } from '@/types/ws';

export function useSuggestions(status: string = 'pending', repoName: string = '') {
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);

  const reload = useCallback(() => {
    wsClient.send({
      type: 'load_suggestions',
      status: status || undefined,
      repo_name: repoName || undefined,
    });
  }, [status, repoName]);

  useEffect(() => {
    reload();
  }, [reload]);

  useWS('suggestion_list', (e) => {
    setSuggestions(e.suggestions);
  });

  // Optimistically remove a suggestion when the user approves/rejects it.
  // The backend confirms via a "system" message but doesn't push a refreshed
  // list, so we update locally to keep the UI snappy.
  const removeLocally = useCallback((id: number) => {
    setSuggestions((cur) => cur.filter((s) => s.id !== id));
  }, []);

  return { suggestions, reload, removeLocally };
}
