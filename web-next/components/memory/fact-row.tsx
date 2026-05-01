'use client';
import { useState } from 'react';
import type { MemoryFact } from '@/types/api';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import type { FactOp } from '@/hooks/useMemoryBrowser';
import { cn } from '@/lib/utils';

interface Props {
  fact: MemoryFact;
  pending?: FactOp;
  error?: string;
  onEdit: (fact_id: string, content: string) => void;
  onCorrect: (fact_id: string, content: string, reason: string) => void;
  onDelete: (fact_id: string) => void;
}

export function FactRow({ fact, pending, error, onEdit, onCorrect, onDelete }: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(fact.content);
  const [correctOpen, setCorrectOpen] = useState(false);
  const [correctContent, setCorrectContent] = useState(fact.content);
  const [correctReason, setCorrectReason] = useState('');
  const [deleteOpen, setDeleteOpen] = useState(false);

  const isSuperseded = !!fact.valid_until;
  const busy = !!pending;

  return (
    <li
      className={cn(
        'rounded border bg-card p-3 text-sm transition-opacity',
        isSuperseded && 'opacity-60',
        busy && 'opacity-70',
      )}
    >
      <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge variant="outline" className="text-[10px]">
          {fact.kind}
        </Badge>
        {fact.author && <span>· {fact.author}</span>}
        {fact.valid_from && (
          <span title={fact.valid_from}>· {fact.valid_from.slice(0, 10)}</span>
        )}
        {isSuperseded && (
          <Badge variant="secondary" className="text-[10px]">
            superseded
          </Badge>
        )}
      </div>

      {editing ? (
        <div className="space-y-2">
          <Textarea rows={4} value={draft} onChange={(e) => setDraft(e.target.value)} />
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => { setEditing(false); setDraft(fact.content); }}>
              Cancel
            </Button>
            <Button
              size="sm"
              disabled={!draft.trim() || draft === fact.content}
              onClick={() => {
                onEdit(fact.id, draft.trim());
                setEditing(false);
              }}
            >
              Save
            </Button>
          </div>
        </div>
      ) : (
        <p className="whitespace-pre-wrap break-words">{fact.content}</p>
      )}

      {error && <p className="mt-2 text-xs text-destructive">{error}</p>}

      {!editing && !isSuperseded && (
        <div className="mt-2 flex justify-end gap-1">
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => setEditing(true)}>
            Edit
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={busy}
            onClick={() => {
              setCorrectContent(fact.content);
              setCorrectReason('');
              setCorrectOpen(true);
            }}
          >
            Correct
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="text-destructive hover:text-destructive"
            disabled={busy}
            onClick={() => setDeleteOpen(true)}
          >
            Delete
          </Button>
        </div>
      )}

      <Dialog open={correctOpen} onOpenChange={setCorrectOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Correct fact</DialogTitle>
            <DialogDescription>
              Supersede the current content with a corrected version. Add a brief reason so the
              audit trail explains the change.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Textarea
              rows={5}
              value={correctContent}
              onChange={(e) => setCorrectContent(e.target.value)}
              placeholder="New content…"
            />
            <Input
              value={correctReason}
              onChange={(e) => setCorrectReason(e.target.value)}
              placeholder="Reason (e.g. 'updated after team review')"
            />
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setCorrectOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={!correctContent.trim() || !correctReason.trim()}
              onClick={() => {
                onCorrect(fact.id, correctContent.trim(), correctReason.trim());
                setCorrectOpen(false);
              }}
            >
              Save correction
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete fact?</DialogTitle>
            <DialogDescription>
              This marks the fact as no longer current. It stays in the history for audit but is
              hidden from the default view.
            </DialogDescription>
          </DialogHeader>
          <p className="rounded bg-muted p-2 text-xs whitespace-pre-wrap">{fact.content}</p>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDeleteOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                onDelete(fact.id);
                setDeleteOpen(false);
              }}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </li>
  );
}
