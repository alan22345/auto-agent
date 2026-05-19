'use client';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ApiError } from '@/lib/api';
import { useSetRepoSecret } from '@/hooks/useRepoSecrets';

// ADR-019 §9 — modal for adding a new user-managed secret.
// Key is validated client-side: must start with an uppercase letter,
// then only uppercase letters, digits, or underscores, max 128 chars.
const KEY_RE = /^[A-Z][A-Z0-9_]{0,127}$/;

interface Props {
  repoId: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function AddSecretModal({ repoId, open, onOpenChange }: Props) {
  const [key, setKey] = useState('');
  const [value, setValue] = useState('');
  const [keyError, setKeyError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const mutation = useSetRepoSecret(repoId);

  function validateKey(k: string): string | null {
    if (!k.trim()) return 'Key is required.';
    if (!KEY_RE.test(k))
      return 'Key must start with an uppercase letter and contain only A–Z, 0–9, or _.';
    return null;
  }

  function handleKeyChange(e: React.ChangeEvent<HTMLInputElement>) {
    const v = e.target.value.toUpperCase();
    setKey(v);
    setKeyError(validateKey(v));
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const err = validateKey(key);
    if (err) {
      setKeyError(err);
      return;
    }
    setSubmitError(null);
    mutation.mutate(
      { key, value },
      {
        onSuccess: () => {
          setKey('');
          setValue('');
          setKeyError(null);
          onOpenChange(false);
        },
        onError: (raw: unknown) => {
          if (raw instanceof ApiError) setSubmitError(raw.detail);
          else if (raw instanceof Error) setSubmitError(raw.message);
          else setSubmitError('Failed to save secret.');
        },
      },
    );
  }

  function handleOpenChange(next: boolean) {
    if (!next) {
      // Reset state on close.
      setKey('');
      setValue('');
      setKeyError(null);
      setSubmitError(null);
    }
    onOpenChange(next);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Add secret</DialogTitle>
          <DialogDescription>
            Secrets are encrypted at rest. Values are write-only — you can reveal a stored
            value after saving.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="add-secret-key">Key</Label>
            <Input
              id="add-secret-key"
              value={key}
              onChange={handleKeyChange}
              placeholder="MY_API_KEY"
              autoComplete="off"
              spellCheck={false}
            />
            {keyError && (
              <p role="alert" className="text-xs text-destructive">
                {keyError}
              </p>
            )}
            <p className="text-xs text-muted-foreground">
              Format: <code>^[A-Z][A-Z0-9_]{'{0,127}'}</code>
            </p>
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="add-secret-value">Value</Label>
            <Input
              id="add-secret-value"
              type="password"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="Write-only — not shown after saving"
              autoComplete="new-password"
            />
          </div>

          {submitError && (
            <p role="alert" className="text-xs text-destructive">
              {submitError}
            </p>
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="secondary"
              onClick={() => handleOpenChange(false)}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={!!keyError || !key || mutation.isPending}>
              {mutation.isPending ? 'Saving…' : 'Save secret'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
