'use client';
import { useState } from 'react';
import Link from 'next/link';
import { useMutation, useQueryClient } from '@tanstack/react-query';
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { ApiError } from '@/lib/api';
import { enableRepoGraph } from '@/lib/code-graph';
import { codeGraphKeys, useOrgRepos } from '@/hooks/useCodeGraphConfigs';
import type { RepoData } from '@/types/api';

interface Props {
  // Repos already graph-enabled — we hide them from the picker so the user
  // can't try to "enable" something already enabled. The endpoint is
  // idempotent so this is a UX nicety, not a correctness guard.
  alreadyEnabledRepoIds: number[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function OnboardModal({ alreadyEnabledRepoIds, open, onOpenChange }: Props) {
  const reposQuery = useOrgRepos();
  const [selectedRepoId, setSelectedRepoId] = useState<string>('');
  const [branchOverride, setBranchOverride] = useState('');
  const [error, setError] = useState<string | null>(null);
  const qc = useQueryClient();

  const repos = reposQuery.data ?? [];
  const candidates = repos.filter((r: RepoData) => !alreadyEnabledRepoIds.includes(r.id));

  const mutation = useMutation({
    mutationFn: async () => {
      const repoId = Number(selectedRepoId);
      if (!repoId) throw new Error('Pick a repo first.');
      return enableRepoGraph(repoId, branchOverride.trim() ? { analysis_branch: branchOverride.trim() } : {});
    },
    onSuccess: () => {
      setError(null);
      setSelectedRepoId('');
      setBranchOverride('');
      qc.invalidateQueries({ queryKey: codeGraphKeys.configs });
      onOpenChange(false);
    },
    onError: (err: unknown) => {
      if (err instanceof ApiError) setError(err.detail);
      else if (err instanceof Error) setError(err.message);
      else setError('Failed to enable graph analysis.');
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    mutation.mutate();
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Enable graph analysis</DialogTitle>
          <DialogDescription>
            Pick an existing repo to graph-enable, or add a new one first.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="onboard-repo">Repo</Label>
            {reposQuery.isLoading ? (
              <p className="text-sm text-muted-foreground">Loading repos…</p>
            ) : candidates.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                {repos.length === 0
                  ? 'No repos yet. '
                  : 'Every repo is already graph-enabled. '}
                <Link href="/settings/integrations/github" className="underline">
                  Add a new repo
                </Link>{' '}
                in settings, then come back here.
              </p>
            ) : (
              <Select value={selectedRepoId} onValueChange={setSelectedRepoId}>
                <SelectTrigger id="onboard-repo">
                  <SelectValue placeholder="Select a repo" />
                </SelectTrigger>
                <SelectContent>
                  {candidates.map((r) => (
                    <SelectItem key={r.id} value={String(r.id)}>
                      {r.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor="onboard-branch">Analysis branch (optional)</Label>
            <Input
              id="onboard-branch"
              value={branchOverride}
              onChange={(e) => setBranchOverride(e.target.value)}
              placeholder="defaults to the repo's default branch"
            />
            <p className="text-xs text-muted-foreground">
              Leave blank to use the repo&apos;s default branch.
            </p>
          </div>

          {error && (
            <p role="alert" className="text-xs text-destructive">
              {error}
            </p>
          )}

          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={!selectedRepoId || mutation.isPending || candidates.length === 0}
            >
              {mutation.isPending ? 'Enabling…' : 'Enable'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
