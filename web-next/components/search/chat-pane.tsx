'use client';
import { useEffect, useRef, useState } from 'react';
import { getSession, type SearchMessage } from '@/lib/search';
import { useSearchStream } from '@/hooks/useSearchStream';
import { MessageBubble, TokenBadge } from './message-bubble';
import { Composer } from './composer';
import { SourceList } from './source-list';
import { MemoryHits } from './memory-hits';

export function ChatPane({
  sessionId,
  onTitleChange,
}: {
  sessionId: number;
  onTitleChange?: (title: string) => void;
}) {
  const [messages, setMessages] = useState<SearchMessage[]>([]);
  const stream = useSearchStream();
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    stream.reset();
    getSession(sessionId).then((s) => {
      if (cancelled) return;
      setMessages(s.messages);
      onTitleChange?.(s.title);
    }).catch(() => {});
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  useEffect(() => {
    if (stream.status !== 'done') return;
    getSession(sessionId).then((s) => {
      setMessages(s.messages);
      onTitleChange?.(s.title);
    }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stream.status]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, stream.answer, stream.sources.length, stream.memoryHits.length]);

  const send = async (content: string) => {
    setMessages((m) => [
      ...m,
      {
        id: -Date.now(),
        role: 'user',
        content,
        tool_events: [],
        truncated: false,
        input_tokens: 0,
        output_tokens: 0,
        created_at: new Date().toISOString(),
      },
    ]);
    await stream.send(sessionId, content);
  };

  const streaming = stream.status === 'streaming';

  return (
    <div className="flex h-full flex-col">
      <div ref={scrollRef} className="flex-1 space-y-4 overflow-auto p-4">
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} />
        ))}

        {streaming && (
          <div className="max-w-[90%]">
            {stream.activeTool && (
              <div className="mb-2 text-xs text-muted-foreground">
                {labelForTool(stream.activeTool.tool, stream.activeTool.args)}
              </div>
            )}
            <MemoryHits hits={stream.memoryHits} />
            <SourceList sources={stream.sources} />
            {stream.answer && (
              <div className="prose prose-sm mt-3 max-w-none rounded-lg bg-card px-3 py-2 whitespace-pre-wrap">
                {stream.answer}
              </div>
            )}
            {(stream.inputTokens > 0 || stream.outputTokens > 0) && (
              <TokenBadge input={stream.inputTokens} output={stream.outputTokens} />
            )}
            {!stream.answer && !stream.activeTool && (
              <div className="text-sm text-muted-foreground">Thinking…</div>
            )}
          </div>
        )}

        {stream.status === 'error' && (
          <div className="rounded border border-destructive bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {stream.error}
          </div>
        )}
      </div>
      <Composer
        onSubmit={send}
        onStop={stream.stop}
        streaming={streaming}
        disabled={streaming}
      />
    </div>
  );
}

function labelForTool(tool: string, args: Record<string, unknown>): string {
  switch (tool) {
    case 'web_search': return `Searching: ${args.query ?? ''}`;
    case 'fetch_url': return `Reading ${args.url ?? ''}`;
    case 'recall_memory': return `Recalling team memory: ${args.query ?? ''}`;
    case 'remember_memory': return `Saving to team memory…`;
    default: return `Running ${tool}…`;
  }
}
