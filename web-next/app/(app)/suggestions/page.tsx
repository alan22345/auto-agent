'use client';
import { useMemo, useState } from 'react';
import { useSuggestions } from '@/hooks/useSuggestions';
import { useFreeformConfigs } from '@/hooks/useFreeformConfigs';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { wsClient } from '@/lib/ws';
import type { Suggestion } from '@/types/ws';
import { MarketBriefModal } from '@/components/market-brief/market-brief-modal';

const STATUS_OPTIONS = [
  { value: 'pending', label: 'Pending' },
  { value: 'approved', label: 'Approved' },
  { value: 'rejected', label: 'Rejected' },
  { value: '', label: 'All statuses' },
];

const PRIORITY_LABEL: Record<number, { label: string; variant: 'default' | 'secondary' | 'outline' }> = {
  1: { label: 'P1', variant: 'default' },
  2: { label: 'P2', variant: 'secondary' },
  3: { label: 'P3', variant: 'outline' },
  4: { label: 'P4', variant: 'outline' },
  5: { label: 'P5', variant: 'outline' },
};

// Maps the suggestion `category` (the only signal the API exposes) into a
// human-friendly source pill. `architecture` is produced by the improvement
// agent (the codebase-deepening loop — ADR-015 §14 renamed); everything
// else comes from the PO analyzer.
function categoryPill(category: string): { source: 'Improvement' | 'PO'; label: string; className: string } {
  if (category === 'architecture') {
    return {
      source: 'Improvement',
      label: 'Improvement · architecture',
      className: 'bg-purple-500/15 text-purple-300 border-purple-500/30',
    };
  }
  return {
    source: 'PO',
    label: `PO · ${category || 'uncategorized'}`,
    className: 'bg-sky-500/15 text-sky-300 border-sky-500/30',
  };
}

export default function SuggestionsPage() {
  const [status, setStatus] = useState('pending');
  const [repo, setRepo] = useState<string>('');
  const [source, setSource] = useState<'all' | 'architect' | 'po'>('all');
  const [briefOpen, setBriefOpen] = useState(false);
  const { suggestions: rawSuggestions, removeLocally } = useSuggestions(status, repo);
  const suggestions = useMemo(() => {
    if (source === 'all') return rawSuggestions;
    if (source === 'architect') return rawSuggestions.filter((s) => s.category === 'architecture');
    return rawSuggestions.filter((s) => s.category !== 'architecture');
  }, [rawSuggestions, source]);
  const configs = useFreeformConfigs();

  const repoOptions = useMemo(() => {
    const fromConfigs = configs.map((c) => c.repo_name);
    const fromSuggestions = suggestions.map((s) => s.repo_name).filter(Boolean) as string[];
    return Array.from(new Set([...fromConfigs, ...fromSuggestions])).sort();
  }, [configs, suggestions]);

  // Derive the numeric repo_id for the selected repo from the loaded suggestions.
  // Used to open the market brief modal for a specific repo.
  const selectedRepoId = useMemo<number | null>(() => {
    if (!repo) return null;
    const match = rawSuggestions.find((s) => s.repo_name === repo && s.repo_id != null);
    return match?.repo_id ?? null;
  }, [repo, rawSuggestions]);

  const grouped = useMemo(() => {
    const byCategory = new Map<string, Suggestion[]>();
    for (const s of suggestions) {
      const key = s.category || 'uncategorized';
      const arr = byCategory.get(key) ?? [];
      arr.push(s);
      byCategory.set(key, arr);
    }
    for (const arr of byCategory.values()) {
      arr.sort((a, b) => a.priority - b.priority || a.id - b.id);
    }
    return Array.from(byCategory.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [suggestions]);

  function approve(id: number) {
    wsClient.send({ type: 'approve_suggestion', suggestion_id: id });
    removeLocally(id);
  }

  function reject(id: number) {
    wsClient.send({ type: 'reject_suggestion', suggestion_id: id });
    removeLocally(id);
  }

  return (
    <div className="h-full overflow-auto">
      <div className="p-6 max-w-5xl mx-auto">
      <h1 className="text-2xl font-semibold mb-1">Improvement suggestions</h1>
      <p className="text-sm text-muted-foreground mb-6">
        Suggestions produced by the PO and improvement agents. Approving creates a freeform task.
      </p>

      <div className="flex items-center gap-3 mb-6 flex-wrap">
        <div className="w-48">
          <Select value={status || '__all__'} onValueChange={(v) => setStatus(v === '__all__' ? '' : v)}>
            <SelectTrigger>
              <SelectValue placeholder="Status" />
            </SelectTrigger>
            <SelectContent>
              {STATUS_OPTIONS.map((o) => (
                <SelectItem key={o.value || '__all__'} value={o.value || '__all__'}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="w-44">
          <Select value={source} onValueChange={(v) => setSource(v as 'all' | 'architect' | 'po')}>
            <SelectTrigger>
              <SelectValue placeholder="Source" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All sources</SelectItem>
              <SelectItem value="architect">Improvement only</SelectItem>
              <SelectItem value="po">PO only</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="w-64">
          <Select value={repo || '__all__'} onValueChange={(v) => setRepo(v === '__all__' ? '' : v)}>
            <SelectTrigger>
              <SelectValue placeholder="Repo" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all__">All repos</SelectItem>
              {repoOptions.map((r) => (
                <SelectItem key={r} value={r}>
                  {r}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {selectedRepoId != null && (
          <>
            <button
              type="button"
              onClick={() => setBriefOpen(true)}
              className="text-sm text-blue-600 hover:underline"
            >
              View market brief
            </button>
            <MarketBriefModal
              repoId={selectedRepoId}
              open={briefOpen}
              onOpenChange={setBriefOpen}
            />
          </>
        )}
      </div>

      {suggestions.length === 0 && (
        <p className="text-sm text-muted-foreground">No suggestions match the current filters.</p>
      )}

      <div className="space-y-8">
        {grouped.map(([category, items]) => (
          <section key={category}>
            <h2 className="text-base font-medium mb-3 capitalize">
              {category} <span className="text-muted-foreground font-normal">({items.length})</span>
            </h2>
            <div className="space-y-3">
              {items.map((s) => (
                <SuggestionCard
                  key={s.id}
                  suggestion={s}
                  onApprove={() => approve(s.id)}
                  onReject={() => reject(s.id)}
                  showActions={s.status === 'pending'}
                />
              ))}
            </div>
          </section>
        ))}
      </div>
      </div>
    </div>
  );
}

function SuggestionCard({
  suggestion,
  onApprove,
  onReject,
  showActions,
}: {
  suggestion: Suggestion;
  onApprove: () => void;
  onReject: () => void;
  showActions: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const priority = PRIORITY_LABEL[suggestion.priority] ?? { label: `P${suggestion.priority}`, variant: 'outline' as const };
  const pill = categoryPill(suggestion.category);

  return (
    <div className="rounded-md border border-border bg-card p-4">
      <div className="flex items-start justify-between gap-3 mb-1.5">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1 flex-wrap">
            <Badge variant={priority.variant}>{priority.label}</Badge>
            <Badge variant="outline" className={pill.className}>{pill.label}</Badge>
            {suggestion.repo_name && (
              <Badge variant="outline" className="font-mono text-[10px]">
                {suggestion.repo_name}
              </Badge>
            )}
            {suggestion.status !== 'pending' && (
              <Badge variant="secondary" className="capitalize">
                {suggestion.status}
              </Badge>
            )}
            {suggestion.task_id && (
              <span className="text-xs text-muted-foreground">→ task #{suggestion.task_id}</span>
            )}
          </div>
          <h3 className="text-sm font-semibold leading-snug">{suggestion.title}</h3>
        </div>
        {showActions && (
          <div className="flex gap-2 shrink-0">
            <Button size="sm" onClick={onApprove}>Approve</Button>
            <Button size="sm" variant="secondary" onClick={onReject}>Reject</Button>
          </div>
        )}
      </div>

      {(suggestion.description || suggestion.rationale) && (
        <>
          <button
            type="button"
            onClick={() => setExpanded((e) => !e)}
            className="text-xs text-muted-foreground hover:text-foreground mt-1"
          >
            {expanded ? 'Hide details' : 'Show details'}
          </button>
          {expanded && (
            <div className="mt-3 space-y-3 text-sm">
              {suggestion.description && (
                <div>
                  <p className="text-xs font-medium text-muted-foreground mb-1">Description</p>
                  <p className="whitespace-pre-wrap leading-relaxed">{suggestion.description}</p>
                </div>
              )}
              {suggestion.rationale && (
                <div>
                  <p className="text-xs font-medium text-muted-foreground mb-1">Rationale</p>
                  <p className="whitespace-pre-wrap leading-relaxed">{suggestion.rationale}</p>
                </div>
              )}
            </div>
          )}
        </>
      )}

      {suggestion.evidence_urls && suggestion.evidence_urls.length > 0 && (
        <div className="mt-3 pt-3 border-t text-xs">
          <span className="text-muted-foreground mr-2">Backed by:</span>
          {suggestion.evidence_urls.slice(0, 3).map((e, i) => (
            <a
              key={i}
              href={e.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-600 hover:underline mr-3"
              title={e.excerpt || e.url}
            >
              {e.title || (() => { try { return new URL(e.url).hostname; } catch { return e.url; } })()}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}
