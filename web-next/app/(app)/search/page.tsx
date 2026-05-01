'use client';
import { useEffect, useState } from 'react';
import { ChatPane } from '@/components/search/chat-pane';
import { SessionList } from '@/components/search/session-list';
import {
  createSession,
  listSessions,
  type SearchSession,
} from '@/lib/search';

export default function SearchPage() {
  const [sessions, setSessions] = useState<SearchSession[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);

  const refresh = async () => {
    const ss = await listSessions().catch(() => []);
    setSessions(ss);
    if (ss.length && activeId == null) setActiveId(ss[0].id);
  };

  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  const onNew = async () => {
    const s = await createSession();
    setSessions((cur) => [s, ...cur]);
    setActiveId(s.id);
  };

  const onDeleted = (id: number) => {
    setSessions((cur) => cur.filter((s) => s.id !== id));
    if (activeId === id) setActiveId(null);
  };

  const onTitleChange = (title: string) => {
    setSessions((cur) => cur.map((s) => (s.id === activeId ? { ...s, title } : s)));
  };

  const onRenamed = (id: number, title: string) => {
    setSessions((cur) => cur.map((s) => (s.id === id ? { ...s, title } : s)));
  };

  return (
    <div className="flex h-full">
      <SessionList
        sessions={sessions}
        activeId={activeId}
        onSelect={setActiveId}
        onNew={onNew}
        onDeleted={onDeleted}
        onRenamed={onRenamed}
      />
      <section className="flex-1 overflow-hidden">
        {activeId == null ? (
          <div className="flex h-full items-center justify-center">
            <p className="text-sm text-muted-foreground">
              Click <strong>+ New search</strong> to start.
            </p>
          </div>
        ) : (
          <ChatPane key={activeId} sessionId={activeId} onTitleChange={onTitleChange} />
        )}
      </section>
    </div>
  );
}
