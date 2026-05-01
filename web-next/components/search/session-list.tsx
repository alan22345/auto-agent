'use client';
import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Pencil } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { SearchSession } from '@/lib/search';
import { deleteSession, renameSession } from '@/lib/search';

export function SessionList({
  sessions,
  activeId,
  onSelect,
  onNew,
  onDeleted,
  onRenamed,
}: {
  sessions: SearchSession[];
  activeId: number | null;
  onSelect: (id: number) => void;
  onNew: () => void;
  onDeleted: (id: number) => void;
  onRenamed: (id: number, title: string) => void;
}) {
  const [editingId, setEditingId] = useState<number | null>(null);
  const [draft, setDraft] = useState('');

  const startEdit = (s: SearchSession) => {
    setEditingId(s.id);
    setDraft(s.title);
  };

  const cancelEdit = () => {
    setEditingId(null);
    setDraft('');
  };

  const commit = async (s: SearchSession) => {
    const next = draft.trim();
    if (!next || next === s.title) {
      cancelEdit();
      return;
    }
    // Optimistic update; revert on failure.
    onRenamed(s.id, next);
    cancelEdit();
    try {
      await renameSession(s.id, next);
    } catch {
      onRenamed(s.id, s.title);
    }
  };

  return (
    <aside className="flex h-full w-64 flex-col border-r bg-card">
      <div className="p-2">
        <Button className="w-full" onClick={onNew}>+ New search</Button>
      </div>
      <ul className="flex-1 overflow-auto">
        {sessions.map((s) => (
          <SessionRow
            key={s.id}
            session={s}
            active={activeId === s.id}
            editing={editingId === s.id}
            draft={draft}
            onSelect={() => onSelect(s.id)}
            onStartEdit={() => startEdit(s)}
            onDraftChange={setDraft}
            onCommit={() => commit(s)}
            onCancel={cancelEdit}
            onDelete={async () => {
              await deleteSession(s.id).catch(() => {});
              onDeleted(s.id);
            }}
          />
        ))}
      </ul>
    </aside>
  );
}

function SessionRow({
  session,
  active,
  editing,
  draft,
  onSelect,
  onStartEdit,
  onDraftChange,
  onCommit,
  onCancel,
  onDelete,
}: {
  session: SearchSession;
  active: boolean;
  editing: boolean;
  draft: string;
  onSelect: () => void;
  onStartEdit: () => void;
  onDraftChange: (v: string) => void;
  onCommit: () => void;
  onCancel: () => void;
  onDelete: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [editing]);

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      onCommit();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      onCancel();
    }
  };

  return (
    <li
      className={cn(
        'group flex items-center justify-between gap-2 px-3 py-2 text-sm hover:bg-secondary',
        !editing && 'cursor-pointer',
        active && 'bg-secondary',
      )}
      onClick={editing ? undefined : onSelect}
      onDoubleClick={(e) => { e.stopPropagation(); onStartEdit(); }}
    >
      {editing ? (
        <input
          ref={inputRef}
          value={draft}
          onChange={(e) => onDraftChange(e.target.value)}
          onKeyDown={onKey}
          onBlur={onCommit}
          onClick={(e) => e.stopPropagation()}
          className="min-w-0 flex-1 rounded border bg-background px-1 py-0.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
        />
      ) : (
        <>
          <span className="min-w-0 flex-1 truncate" title={session.title}>{session.title}</span>
          <div className="flex shrink-0 items-center gap-1 opacity-0 group-hover:opacity-100">
            <button
              type="button"
              aria-label="Rename"
              className="rounded p-0.5 text-muted-foreground hover:text-foreground"
              onClick={(e) => { e.stopPropagation(); onStartEdit(); }}
            >
              <Pencil size={12} />
            </button>
            <button
              type="button"
              aria-label="Delete"
              className="rounded px-1 text-xs text-muted-foreground hover:text-destructive"
              onClick={(e) => { e.stopPropagation(); onDelete(); }}
            >
              ×
            </button>
          </div>
        </>
      )}
    </li>
  );
}
