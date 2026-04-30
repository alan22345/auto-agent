'use client';
import { useState, KeyboardEvent } from 'react';
import { Button } from '@/components/ui/button';

export function Composer({
  disabled,
  onSubmit,
  onStop,
  streaming,
}: {
  disabled?: boolean;
  onSubmit: (content: string) => void;
  onStop?: () => void;
  streaming?: boolean;
}) {
  const [text, setText] = useState('');

  const submit = () => {
    const t = text.trim();
    if (!t || disabled) return;
    onSubmit(t);
    setText('');
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="flex items-end gap-2 border-t p-3">
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKey}
        rows={2}
        placeholder="Ask anything…"
        className="flex-1 resize-none rounded border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
        disabled={disabled}
      />
      {streaming && onStop ? (
        <Button variant="secondary" onClick={onStop}>Stop</Button>
      ) : (
        <Button onClick={submit} disabled={disabled || !text.trim()}>Send</Button>
      )}
    </div>
  );
}
