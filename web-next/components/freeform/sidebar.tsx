'use client';
import { Button } from '@/components/ui/button';
import { api, ApiError } from '@/lib/api';
import type { FreeformConfig } from '@/types/ws';

export type FreeformView =
  | { kind: 'build' }
  | { kind: 'existing' }
  | { kind: 'repo'; name: string }
  | null;

interface Props {
  configs: FreeformConfig[];
  view: FreeformView;
  onView: (v: FreeformView) => void;
  onConfigsChange?: (configs: FreeformConfig[]) => void;
}

// Mirrors renderFreeformSidebar() in web/static/index.html ~line 1169
export function FreeformSidebar({ configs, view, onView, onConfigsChange }: Props) {
  const sorted = [...configs].sort((a, b) => {
    if (a.enabled !== b.enabled) return Number(b.enabled) - Number(a.enabled);
    return (a.repo_name ?? '').localeCompare(b.repo_name ?? '');
  });

  async function handleDelete(repoName: string) {
    if (
      !confirm(
        `Remove ${repoName}? This will disable freeform mode and remove it from the dashboard. Existing tasks will not be deleted.`,
      )
    )
      return;
    try {
      await api(`/api/repos/${encodeURIComponent(repoName)}`, { method: 'DELETE' });
      if (onConfigsChange) {
        onConfigsChange(configs.filter((c) => c.repo_name !== repoName));
      }
      // If we were viewing this repo, switch to existing-repo view
      if (view && view.kind === 'repo' && view.name === repoName) {
        onView({ kind: 'existing' });
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        alert(err.detail || 'Cannot delete: active tasks reference this repo.');
      } else if (err instanceof ApiError) {
        alert(err.detail || 'Failed to delete repo.');
      } else {
        alert('Error deleting repo: ' + String(err));
      }
    }
  }

  return (
    <aside className="w-56 shrink-0 border-r border-border bg-card flex flex-col overflow-y-auto">
      {/* Action buttons */}
      <div className="p-3 flex flex-col gap-1 border-b border-border">
        <Button
          variant={view?.kind === 'build' ? 'secondary' : 'ghost'}
          size="sm"
          className="justify-start text-sm"
          onClick={() => onView({ kind: 'build' })}
        >
          Build something new
        </Button>
        <Button
          variant={view?.kind === 'existing' ? 'secondary' : 'ghost'}
          size="sm"
          className="justify-start text-sm"
          onClick={() => onView({ kind: 'existing' })}
        >
          Add to existing
        </Button>
      </div>

      {/* Repo cards */}
      <div className="flex-1 p-2 flex flex-col gap-1">
        {sorted.length === 0 ? (
          <p className="text-xs text-muted-foreground text-center py-4">No freeform repos yet</p>
        ) : (
          sorted.map((cfg) => {
            const isSelected = view?.kind === 'repo' && view.name === cfg.repo_name;
            return (
              <div
                key={cfg.repo_name}
                role="button"
                tabIndex={0}
                onClick={() => onView({ kind: 'repo', name: cfg.repo_name })}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ')
                    onView({ kind: 'repo', name: cfg.repo_name });
                }}
                className={`group relative rounded-md px-3 py-2 cursor-pointer text-sm transition-colors ${
                  isSelected
                    ? 'bg-accent text-accent-foreground'
                    : 'hover:bg-accent/50 text-foreground'
                }`}
              >
                {/* Delete button — visible on hover */}
                <button
                  className="absolute right-1 top-1 hidden group-hover:flex items-center justify-center w-5 h-5 rounded text-muted-foreground hover:text-destructive"
                  title="Remove repo"
                  onClick={(e) => {
                    e.stopPropagation();
                    handleDelete(cfg.repo_name);
                  }}
                >
                  &times;
                </button>
                <div className="font-medium truncate pr-4">{cfg.repo_name || '(unnamed)'}</div>
                <div className="flex items-center gap-1 mt-0.5 text-xs text-muted-foreground">
                  <span
                    className={`inline-block w-2 h-2 rounded-full ${cfg.enabled ? 'bg-green-500' : 'bg-muted-foreground'}`}
                  />
                  {cfg.enabled ? 'enabled' : 'disabled'}
                  {cfg.auto_approve_suggestions && ' · auto-approve'}
                </div>
              </div>
            );
          })
        )}
      </div>
    </aside>
  );
}
