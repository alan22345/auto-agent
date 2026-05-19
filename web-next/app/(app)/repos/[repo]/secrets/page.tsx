'use client';
import { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import { ArrowLeft, CheckCircle2, Circle, Eye, EyeOff, Plus, Pencil, Trash2, FlaskConical } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { ApiError } from '@/lib/api';
import { useRepos } from '@/hooks/useRepos';
import {
  useRepoSecrets,
  useSetRepoSecret,
  useClearRepoSecret,
  useDeleteRepoSecret,
  useRevealRepoSecret,
  useTestRepoSecret,
} from '@/hooks/useRepoSecrets';
import { AddSecretModal } from '@/components/secrets/AddSecretModal';
import type { RepoSecretEntry } from '@/lib/repo-secrets';

// ADR-019 §9 — per-repo secrets management page.

// How long (ms) a revealed value stays visible before re-masking.
const REVEAL_TIMEOUT_MS = 10_000;

interface RevealState {
  key: string;
  value: string;
  expiresAt: number;
}

interface EditModalState {
  key: string;
  open: boolean;
}

interface ConfirmRevealState {
  key: string;
  open: boolean;
}

function SecretRow({
  entry,
  repoId,
  revealState,
  onRevealRequest,
  onEdit,
  onDelete,
}: {
  entry: RepoSecretEntry;
  repoId: number;
  revealState: RevealState | null;
  onRevealRequest: (key: string) => void;
  onEdit: (key: string) => void;
  onDelete: (key: string) => void;
}) {
  const clearMutation = useClearRepoSecret(repoId);
  const testMutation = useTestRepoSecret(repoId);
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [clearError, setClearError] = useState<string | null>(null);

  const isRevealed = revealState?.key === entry.key;
  const revealedValue = isRevealed ? revealState!.value : null;

  function handleClear() {
    setClearError(null);
    clearMutation.mutate(entry.key, {
      onError: (raw: unknown) => {
        if (raw instanceof ApiError) setClearError(raw.detail);
        else if (raw instanceof Error) setClearError(raw.message);
        else setClearError('Failed to clear secret.');
      },
    });
  }

  function handleTest() {
    setTestResult(null);
    testMutation.mutate(entry.key, {
      onSuccess: (res) => setTestResult({ ok: res.ok, message: res.message }),
      onError: (raw: unknown) => {
        const msg =
          raw instanceof ApiError
            ? raw.detail
            : raw instanceof Error
              ? raw.message
              : 'Test failed.';
        setTestResult({ ok: false, message: msg });
      },
    });
  }

  return (
    <li className="flex flex-col gap-1 border-b px-4 py-3 last:border-b-0">
      <div className="flex flex-wrap items-start gap-2">
        {/* Status dot */}
        <span className="mt-0.5 shrink-0">
          {entry.is_set ? (
            <CheckCircle2 size={15} className="text-green-500" aria-label="Set" />
          ) : (
            <Circle size={15} className="text-destructive" aria-label="Not set" />
          )}
        </span>

        {/* Key + purpose */}
        <div className="flex min-w-0 flex-1 flex-col">
          <span className="font-mono text-sm font-medium">{entry.key}</span>
          {entry.purpose && (
            <span className="text-xs text-muted-foreground">{entry.purpose}</span>
          )}
          {entry.is_set && !isRevealed && (
            <span className="mt-1 font-mono text-xs text-muted-foreground">••••••••</span>
          )}
          {entry.is_set && isRevealed && revealedValue !== null && (
            <span className="mt-1 break-all font-mono text-xs">{revealedValue}</span>
          )}
          {entry.is_set && isRevealed && revealedValue === null && (
            <span className="mt-1 text-xs text-muted-foreground italic">(empty value)</span>
          )}
          {testResult && (
            <span
              className={`mt-1 text-xs ${testResult.ok ? 'text-green-600' : 'text-destructive'}`}
            >
              {testResult.ok ? '✓' : '✗'} {testResult.message}
            </span>
          )}
          {clearError && (
            <span role="alert" className="mt-1 text-xs text-destructive">
              {clearError}
            </span>
          )}
        </div>

        {/* Action buttons */}
        <div className="flex shrink-0 flex-wrap items-center gap-1">
          {!entry.is_set && (
            <Button size="sm" variant="default" onClick={() => onEdit(entry.key)}>
              Set
            </Button>
          )}
          {entry.is_set && (
            <>
              <Button
                size="sm"
                variant="outline"
                onClick={() => onRevealRequest(entry.key)}
                title={isRevealed ? 'Re-mask' : 'Reveal value'}
              >
                {isRevealed ? <EyeOff size={14} /> : <Eye size={14} />}
              </Button>
              <Button size="sm" variant="outline" onClick={() => onEdit(entry.key)}>
                <Pencil size={14} className="mr-1" />
                Edit
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={handleClear}
                disabled={clearMutation.isPending}
              >
                Clear
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={handleTest}
                disabled={testMutation.isPending}
                title="Test connectivity"
              >
                <FlaskConical size={14} className="mr-1" />
                {testMutation.isPending ? '…' : 'Test'}
              </Button>
            </>
          )}
          {entry.source === 'user' && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => onDelete(entry.key)}
              className="text-destructive hover:text-destructive"
              title="Delete secret"
            >
              <Trash2 size={14} />
            </Button>
          )}
        </div>
      </div>
    </li>
  );
}

function Section({
  title,
  entries,
  repoId,
  revealState,
  onRevealRequest,
  onEdit,
  onDelete,
  emptyMessage,
}: {
  title: string;
  entries: RepoSecretEntry[];
  repoId: number;
  revealState: RevealState | null;
  onRevealRequest: (key: string) => void;
  onEdit: (key: string) => void;
  onDelete: (key: string) => void;
  emptyMessage: string;
}) {
  return (
    <section className="rounded-lg border bg-card/40">
      <div className="border-b px-4 py-3">
        <h2 className="text-sm font-semibold">{title}</h2>
      </div>
      {entries.length === 0 ? (
        <p className="px-4 py-4 text-sm text-muted-foreground">{emptyMessage}</p>
      ) : (
        <ul>
          {entries.map((entry) => (
            <SecretRow
              key={entry.key}
              entry={entry}
              repoId={repoId}
              revealState={revealState?.key === entry.key ? revealState : null}
              onRevealRequest={onRevealRequest}
              onEdit={onEdit}
              onDelete={onDelete}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

// Modal for editing an existing secret's value (key is fixed).
function EditSecretModal({
  repoId,
  secretKey,
  open,
  onOpenChange,
}: {
  repoId: number;
  secretKey: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [value, setValue] = useState('');
  const [error, setError] = useState<string | null>(null);
  const mutation = useSetRepoSecret(repoId);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    mutation.mutate(
      { key: secretKey, value },
      {
        onSuccess: () => {
          setValue('');
          onOpenChange(false);
        },
        onError: (raw: unknown) => {
          if (raw instanceof ApiError) setError(raw.detail);
          else if (raw instanceof Error) setError(raw.message);
          else setError('Failed to save secret.');
        },
      },
    );
  }

  function handleOpenChange(next: boolean) {
    if (!next) {
      setValue('');
      setError(null);
    }
    onOpenChange(next);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Set value for {secretKey}</DialogTitle>
          <DialogDescription>
            The new value replaces the existing one. Values are write-only.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Input
              type="password"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="New value"
              autoComplete="new-password"
              autoFocus
            />
          </div>
          {error && (
            <p role="alert" className="text-xs text-destructive">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => handleOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={mutation.isPending}>
              {mutation.isPending ? 'Saving…' : 'Save'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// Confirmation dialog before reveal.
function ConfirmRevealDialog({
  secretKey,
  open,
  onConfirm,
  onCancel,
  isPending,
}: {
  secretKey: string;
  open: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  isPending: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={(v) => !v && onCancel()}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Reveal secret?</DialogTitle>
          <DialogDescription>
            Are you sure you want to reveal <strong>{secretKey}</strong> in your browser? This
            action will be audit-logged.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="secondary" onClick={onCancel} disabled={isPending}>
            Cancel
          </Button>
          <Button onClick={onConfirm} disabled={isPending}>
            {isPending ? 'Revealing…' : 'Reveal'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// Delete confirmation dialog.
function ConfirmDeleteDialog({
  secretKey,
  open,
  onConfirm,
  onCancel,
  isPending,
}: {
  secretKey: string;
  open: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  isPending: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={(v) => !v && onCancel()}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Delete secret?</DialogTitle>
          <DialogDescription>
            This permanently removes <strong>{secretKey}</strong>. This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="secondary" onClick={onCancel} disabled={isPending}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={onConfirm} disabled={isPending}>
            {isPending ? 'Deleting…' : 'Delete'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function RepoSecretsPage({
  params,
  searchParams,
}: {
  params: { repo: string };
  searchParams?: { filter?: string };
}) {
  const repoId = Number(params.repo);
  const filterArchitect = searchParams?.filter === 'architect_required';

  const { data: repos } = useRepos();
  const repo = repos?.find((r) => r.id === repoId);
  const { data, isLoading, isError, error } = useRepoSecrets(
    Number.isFinite(repoId) ? repoId : null,
  );

  const [addOpen, setAddOpen] = useState(false);
  const [editModal, setEditModal] = useState<EditModalState>({ key: '', open: false });
  const [confirmReveal, setConfirmReveal] = useState<ConfirmRevealState>({
    key: '',
    open: false,
  });
  const [confirmDelete, setConfirmDelete] = useState<{ key: string; open: boolean }>({
    key: '',
    open: false,
  });
  const [revealState, setRevealState] = useState<RevealState | null>(null);
  const [revealError, setRevealError] = useState<string | null>(null);

  const revealMutation = useRevealRepoSecret(repoId);
  const deleteMutation = useDeleteRepoSecret(repoId);

  // Auto-mask after REVEAL_TIMEOUT_MS.
  useEffect(() => {
    if (!revealState) return;
    const remaining = revealState.expiresAt - Date.now();
    if (remaining <= 0) {
      setRevealState(null);
      return;
    }
    const t = setTimeout(() => setRevealState(null), remaining);
    return () => clearTimeout(t);
  }, [revealState]);

  function handleRevealRequest(key: string) {
    // If already revealed for this key, clicking again re-masks.
    if (revealState?.key === key) {
      setRevealState(null);
      return;
    }
    setConfirmReveal({ key, open: true });
  }

  const handleRevealConfirm = useCallback(() => {
    const key = confirmReveal.key;
    setRevealError(null);
    revealMutation.mutate(key, {
      onSuccess: (res) => {
        setRevealState({
          key,
          value: res.value ?? '',
          expiresAt: Date.now() + REVEAL_TIMEOUT_MS,
        });
        setConfirmReveal({ key: '', open: false });
      },
      onError: (raw: unknown) => {
        const msg =
          raw instanceof ApiError
            ? raw.detail
            : raw instanceof Error
              ? raw.message
              : 'Failed to reveal secret.';
        setRevealError(msg);
        setConfirmReveal({ key: '', open: false });
      },
    });
  }, [confirmReveal.key, revealMutation]);

  function handleDeleteRequest(key: string) {
    setConfirmDelete({ key, open: true });
  }

  function handleDeleteConfirm() {
    const key = confirmDelete.key;
    deleteMutation.mutate(key, {
      onSuccess: () => setConfirmDelete({ key: '', open: false }),
    });
  }

  const keys = data?.keys ?? [];
  const architectRequired = keys.filter((k) => k.source === 'architect_required');
  const userSecrets = keys.filter((k) => k.source === 'user');

  const displayArchitect = architectRequired;
  const displayUser = filterArchitect ? [] : userSecrets;

  if (!Number.isFinite(repoId)) {
    return (
      <div className="p-6">
        <p role="alert" className="text-sm text-destructive">
          Invalid repo id.
        </p>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-hidden p-6">
      <header className="mb-6 flex items-center justify-between gap-4">
        <div>
          <Link
            href="/repos"
            className="mb-2 inline-flex items-center text-xs text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft size={14} className="mr-1" /> Back to repos
          </Link>
          <h1 className="text-xl font-semibold">
            Project secrets for {repo?.name ?? decodeURIComponent(params.repo)}
          </h1>
          <p className="text-sm text-muted-foreground">
            Encrypted at rest. Values are never shown in the agent chat or logs.
          </p>
        </div>
        <Button onClick={() => setAddOpen(true)}>
          <Plus size={14} className="mr-2" />
          Add secret
        </Button>
      </header>

      {revealError && (
        <p role="alert" className="mb-4 text-sm text-destructive">
          {revealError}
        </p>
      )}

      <section className="min-h-0 flex-1 space-y-6 overflow-auto">
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading secrets…</p>
        ) : isError ? (
          <p role="alert" className="text-sm text-destructive">
            {error instanceof Error ? error.message : 'Failed to load secrets.'}
          </p>
        ) : (
          <>
            <Section
              title="Required by architects"
              entries={displayArchitect}
              repoId={repoId}
              revealState={revealState}
              onRevealRequest={handleRevealRequest}
              onEdit={(key) => setEditModal({ key, open: true })}
              onDelete={handleDeleteRequest}
              emptyMessage="No architect-required secrets declared for this repo."
            />
            {!filterArchitect && (
              <Section
                title="Other secrets"
                entries={displayUser}
                repoId={repoId}
                revealState={revealState}
                onRevealRequest={handleRevealRequest}
                onEdit={(key) => setEditModal({ key, open: true })}
                onDelete={handleDeleteRequest}
                emptyMessage="No user-added secrets yet. Use '+ Add secret' to add one."
              />
            )}
          </>
        )}
      </section>

      <AddSecretModal repoId={repoId} open={addOpen} onOpenChange={setAddOpen} />

      <EditSecretModal
        repoId={repoId}
        secretKey={editModal.key}
        open={editModal.open}
        onOpenChange={(v) => setEditModal((s) => ({ ...s, open: v }))}
      />

      <ConfirmRevealDialog
        secretKey={confirmReveal.key}
        open={confirmReveal.open}
        onConfirm={handleRevealConfirm}
        onCancel={() => setConfirmReveal({ key: '', open: false })}
        isPending={revealMutation.isPending}
      />

      <ConfirmDeleteDialog
        secretKey={confirmDelete.key}
        open={confirmDelete.open}
        onConfirm={handleDeleteConfirm}
        onCancel={() => setConfirmDelete({ key: '', open: false })}
        isPending={deleteMutation.isPending}
      />
    </div>
  );
}
