'use client';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { MemoryHit, SearchMessage, Source } from '@/lib/search';
import { SourceList } from './source-list';
import { MemoryHits } from './memory-hits';

function extractSources(events: SearchMessage['tool_events']): Source[] {
  return events.filter((e): e is Source & { type: 'source' } => e.type === 'source')
    .map(({ url, title, summary, query }) => ({ url, title, summary, query }));
}
function extractHits(events: SearchMessage['tool_events']): MemoryHit[] {
  return events.filter((e): e is MemoryHit & { type: 'memory_hit' } => e.type === 'memory_hit')
    .map(({ entity, facts }) => ({ entity, facts }));
}

export function MessageBubble({ message }: { message: SearchMessage }) {
  if (message.role === 'user') {
    return (
      <div className="ml-auto max-w-[80%] rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground">
        {message.content}
      </div>
    );
  }
  const sources = extractSources(message.tool_events);
  const hits = extractHits(message.tool_events);
  return (
    <div className="max-w-[90%]">
      <div className="prose prose-sm max-w-none rounded-lg bg-card px-3 py-2">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
      </div>
      <MemoryHits hits={hits} />
      <SourceList sources={sources} />
    </div>
  );
}
