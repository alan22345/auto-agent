import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useSearchStream } from '@/hooks/useSearchStream';

function makeStream(lines: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    async start(controller) {
      for (const l of lines) controller.enqueue(encoder.encode(l + '\n'));
      controller.close();
    },
  });
}

describe('useSearchStream', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('parses tool_call_start, source, text, done', async () => {
    const stream = makeStream([
      JSON.stringify({ type: 'tool_call_start', tool: 'web_search', args: { query: 'x' } }),
      JSON.stringify({ type: 'source', url: 'https://a', title: 'A', summary: 's', query: 'x' }),
      JSON.stringify({ type: 'text', delta: 'Hello ' }),
      JSON.stringify({ type: 'text', delta: 'world.' }),
      JSON.stringify({ type: 'done', answer: 'Hello world.' }),
    ]);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      body: stream,
    }));

    const { result } = renderHook(() => useSearchStream());
    await act(async () => { await result.current.send(1, 'hello'); });

    await waitFor(() => expect(result.current.status).toBe('done'));
    expect(result.current.answer).toBe('Hello world.');
    expect(result.current.sources.map(s => s.url)).toEqual(['https://a']);
  });

  it('captures errors', async () => {
    const stream = makeStream([
      JSON.stringify({ type: 'error', message: 'boom' }),
    ]);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, body: stream }));
    const { result } = renderHook(() => useSearchStream());
    await act(async () => { await result.current.send(1, 'x'); });
    await waitFor(() => expect(result.current.status).toBe('error'));
    expect(result.current.error).toBe('boom');
  });
});
