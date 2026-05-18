'use client';
import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { updateRepoGraphConfig } from '@/lib/code-graph';
import { ApiError } from '@/lib/api';
import { codeGraphKeys } from '@/hooks/useCodeGraphConfigs';
import type { RepoGraphConfigData } from '@/types/api';

type SaveStatus =
  | { kind: 'idle' }
  | { kind: 'saving' }
  | { kind: 'saved' }
  | { kind: 'error'; message: string };

interface Props {
  config: RepoGraphConfigData;
}

// Mirrors PATCH /api/repos/{repo_id}/graph. The text input keeps the same
// validation contract as the existing /repos branch-update flow (alphanumeric
// + . _ / -). Tag/SHA pickers are explicitly deferred by ADR-016
// "Out of scope".
export function BranchPicker({ config }: Props) {
  const [branch, setBranch] = useState(config.analysis_branch);
  const [status, setStatus] = useState<SaveStatus>({ kind: 'idle' });
  const qc = useQueryClient();

  // Re-sync local state when the parent loads a different config.
  useEffect(() => {
    setBranch(config.analysis_branch);
    setStatus({ kind: 'idle' });
  }, [config.repo_id, config.analysis_branch]);

  const mutation = useMutation({
    mutationFn: (analysis_branch: string) =>
      updateRepoGraphConfig(config.repo_id, { analysis_branch }),
    onMutate: () => setStatus({ kind: 'saving' }),
    onSuccess: (data) => {
      setStatus({ kind: 'saved' });
      qc.setQueryData(codeGraphKeys.config(config.repo_id), data);
      qc.invalidateQueries({ queryKey: codeGraphKeys.configs });
    },
    onError: (err: unknown) => {
      const message =
        err instanceof ApiError ? err.detail : err instanceof Error ? err.message : 'Save failed';
      setStatus({ kind: 'error', message });
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = branch.trim();
    if (!trimmed || trimmed === config.analysis_branch) return;
    mutation.mutate(trimmed);
  }

  const dirty = branch.trim() !== config.analysis_branch;

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-2 max-w-md">
      <Label htmlFor="analysis-branch">Analysis branch</Label>
      <div className="flex gap-2">
        <Input
          id="analysis-branch"
          name="analysis_branch"
          value={branch}
          onChange={(e) => setBranch(e.target.value)}
          placeholder="main"
          aria-label="Analysis branch"
        />
        <Button type="submit" disabled={!dirty || status.kind === 'saving'}>
          {status.kind === 'saving' ? 'Saving…' : 'Save'}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        The branch the analyser will refresh against. Changing this marks the existing graph as
        stale; the next refresh picks up the new branch.
      </p>
      {status.kind === 'saved' && (
        <p className="text-xs text-success" role="status">
          Saved.
        </p>
      )}
      {status.kind === 'error' && (
        <p className="text-xs text-destructive" role="alert">
          {status.message}
        </p>
      )}
    </form>
  );
}
