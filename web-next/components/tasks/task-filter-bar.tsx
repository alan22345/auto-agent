'use client';
import { useMemo } from 'react';
import type { TaskData } from '@/types/api';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

export type StatusFilter = 'all' | 'active' | 'needs_action' | 'done' | 'failed';
export type RepoFilter = 'all' | '__none__' | string;

export type TaskFilter = { status: StatusFilter; repo: RepoFilter };

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'active', label: 'Active' },
  { value: 'needs_action', label: 'Needs Action' },
  { value: 'done', label: 'Done' },
  { value: 'failed', label: 'Failed' },
];

export function TaskFilterBar({
  tasks,
  filter,
  onChange,
}: {
  tasks: TaskData[];
  filter: TaskFilter;
  onChange: (next: TaskFilter) => void;
}) {
  const repos = useMemo(() => {
    const set = new Set<string>();
    for (const t of tasks) if (t.repo_name) set.add(t.repo_name);
    return [...set].sort();
  }, [tasks]);

  const hasNoRepo = useMemo(() => tasks.some((t) => !t.repo_name), [tasks]);

  return (
    <div className="flex gap-2 border-b p-2">
      <Select
        value={filter.status}
        onValueChange={(v) => onChange({ ...filter, status: v as StatusFilter })}
      >
        <SelectTrigger className="h-8 text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {STATUS_OPTIONS.map((o) => (
            <SelectItem key={o.value} value={o.value}>
              {o.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <Select
        value={filter.repo}
        onValueChange={(v) => onChange({ ...filter, repo: v })}
      >
        <SelectTrigger className="h-8 text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All repos</SelectItem>
          {repos.map((r) => (
            <SelectItem key={r} value={r}>
              {r}
            </SelectItem>
          ))}
          {hasNoRepo && <SelectItem value="__none__">(no repo)</SelectItem>}
        </SelectContent>
      </Select>
    </div>
  );
}

export function applyTaskFilter(tasks: TaskData[], filter: TaskFilter): TaskData[] {
  return tasks.filter((t) => {
    switch (filter.status) {
      case 'active':
        if (['done', 'failed'].includes(t.status)) return false;
        break;
      case 'needs_action':
        if (!['awaiting_approval', 'awaiting_clarification'].includes(t.status)) return false;
        break;
      case 'done':
        if (t.status !== 'done') return false;
        break;
      case 'failed':
        if (t.status !== 'failed') return false;
        break;
    }
    if (filter.repo === 'all') return true;
    if (filter.repo === '__none__') return !t.repo_name;
    return t.repo_name === filter.repo;
  });
}
