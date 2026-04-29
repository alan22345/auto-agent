'use client';
import { useState, useRef } from 'react';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Input } from '@/components/ui/input';
import { wsClient } from '@/lib/ws';
import { uploadMemoryFile } from '@/lib/memory';
import { cn } from '@/lib/utils';

interface Props {
  sourceId: string | null;
  setSourceId: (id: string | null) => void;
  setStatus: (s: { text: string; isError?: boolean }) => void;
}

export function DropZone({ sourceId, setSourceId, setStatus }: Props) {
  const [paste, setPaste] = useState('');
  const [hint, setHint] = useState('');
  const [busy, setBusy] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  async function uploadAndSet(file: File) {
    setBusy(true);
    setStatus({ text: 'Uploading…' });
    try {
      const id = await uploadMemoryFile(file);
      setSourceId(id);
      setStatus({ text: 'Uploaded. Click Extract.' });
      return id;
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Upload failed';
      setStatus({ text: msg, isError: true });
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function extract() {
    const ctx = hint.trim() || undefined;
    if (sourceId) {
      wsClient.send({ type: 'memory_extract', source_id: sourceId, context_hint: ctx });
    } else if (fileRef.current?.files?.[0]) {
      const id = await uploadAndSet(fileRef.current.files[0]);
      if (!id) return;
      wsClient.send({ type: 'memory_extract', source_id: id, context_hint: ctx });
    } else if (paste.trim()) {
      wsClient.send({ type: 'memory_extract', pasted_text: paste, context_hint: ctx });
    } else {
      setStatus({ text: 'Choose a file or paste some text first.', isError: true });
      return;
    }
    setStatus({ text: 'Extracting…' });
  }

  return (
    <div
      className={cn(
        'rounded-lg border border-dashed p-4 transition-colors',
        dragOver && 'border-accent bg-accent/10',
      )}
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={async (e) => {
        e.preventDefault();
        setDragOver(false);
        const f = e.dataTransfer.files[0];
        if (f) await uploadAndSet(f);
      }}
    >
      <input ref={fileRef} type="file" className="hidden" accept=".md,.txt,.log,.pdf" />
      <div className="mb-3 flex items-center gap-2">
        <Button variant="secondary" onClick={() => fileRef.current?.click()}>
          Choose file
        </Button>
        <span className="text-xs text-muted-foreground">or drag a file here</span>
      </div>
      <p className="mb-2 text-xs text-muted-foreground">or paste text below</p>
      <Textarea
        rows={6}
        placeholder="Paste meeting notes, Slack thread, email body…"
        value={paste}
        onChange={(e) => setPaste(e.target.value)}
      />
      <Input
        className="mt-2"
        placeholder="Context hint (optional) — e.g. 'from Thursday standup'"
        value={hint}
        onChange={(e) => setHint(e.target.value)}
      />
      <Button className="mt-3" onClick={extract} disabled={busy}>
        {busy ? 'Working…' : 'Extract'}
      </Button>
    </div>
  );
}
