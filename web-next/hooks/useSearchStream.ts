'use client';
import { useCallback, useRef, useState } from 'react';
import type { MemoryHit, SearchEvent, Source } from '@/lib/search';

export type StreamStatus = 'idle' | 'streaming' | 'done' | 'error';

export type StreamState = {
  status: StreamStatus;
  activeTool: { tool: string; args: Record<string, unknown> } | null;
  sources: Source[];
  memoryHits: MemoryHit[];
  answer: string;
  inputTokens: number;
  outputTokens: number;
  error: string | null;
};

const initial: StreamState = {
  status: 'idle',
  activeTool: null,
  sources: [],
  memoryHits: [],
  answer: '',
  inputTokens: 0,
  outputTokens: 0,
  error: null,
};

export function useSearchStream() {
  const [state, setState] = useState<StreamState>(initial);
  const abortRef = useRef<AbortController | null>(null);

  const reset = useCallback(() => setState(initial), []);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  const send = useCallback(async (sessionId: number, content: string) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setState({ ...initial, status: 'streaming' });

    try {
      const res = await fetch(`/api/search/sessions/${sessionId}/messages`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
        signal: controller.signal,
      });
      if (!res.ok || !res.body) {
        const detail = await res.text().catch(() => '');
        setState((s) => ({ ...s, status: 'error', error: detail || res.statusText }));
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed) continue;
          try {
            const ev = JSON.parse(trimmed) as SearchEvent;
            setState((s) => apply(s, ev));
          } catch {
            // ignore malformed line
          }
        }
      }
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : 'Stream failed';
      if (controller.signal.aborted) return;
      setState((s) => ({ ...s, status: 'error', error: message }));
    }
  }, []);

  return { ...state, send, stop, reset };
}

function apply(s: StreamState, ev: SearchEvent): StreamState {
  switch (ev.type) {
    case 'tool_call_start':
      return { ...s, activeTool: { tool: ev.tool, args: ev.args } };
    case 'source':
      return { ...s, sources: [...s.sources, { url: ev.url, title: ev.title, summary: ev.summary, query: ev.query }] };
    case 'memory_hit':
      return { ...s, memoryHits: [...s.memoryHits, { entity: ev.entity, facts: ev.facts }] };
    case 'text':
      // 'text' events arrive as full assistant-turn snapshots from agent/loop.py,
      // not streaming deltas. Replace rather than concatenate so the live answer
      // matches the persisted final answer instead of accumulating intermediate
      // tool-call narration ("Let me search…", "Now reading…", final answer).
      return { ...s, answer: ev.delta };
    case 'done':
      return {
        ...s,
        status: 'done',
        activeTool: null,
        answer: ev.answer,
        inputTokens: ev.input_tokens ?? 0,
        outputTokens: ev.output_tokens ?? 0,
      };
    case 'error':
      return { ...s, status: 'error', error: ev.message, activeTool: null };
    default:
      return s;
  }
}
