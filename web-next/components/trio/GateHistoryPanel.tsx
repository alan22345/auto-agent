'use client';
import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useGateHistory } from '@/hooks/useGateApproval';
import { cn } from '@/lib/utils';
import type { GateDecisionOut } from '@/types/api';

// ADR-015 §6 Phase 12 — gate-history audit panel.
//
// Renders every persisted gate decision (user + standin) in oldest-first
// order so the operator can reconstruct who decided what at every gate.
// Source label drives a small per-row tint so user-driven approvals,
// PO-standin decisions, and improvement-agent decisions are easy to
// scan apart at a glance.

const GATE_LABEL: Record<string, string> = {
  grill: 'Grill',
  plan_approval: 'Plan',
  design_approval: 'Design',
  pr_review: 'PR Review',
};

const SOURCE_LABEL: Record<string, string> = {
  user: 'User',
  po_standin: 'PO standin',
  improvement_standin: 'Improvement standin',
};

const SOURCE_TONE: Record<string, string> = {
  user: 'bg-blue-500/15 text-blue-700 dark:text-blue-400',
  po_standin: 'bg-purple-500/15 text-purple-700 dark:text-purple-400',
  improvement_standin: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-400',
};

function verdictTone(verdict: string): string {
  const v = verdict.toLowerCase();
  if (v === 'approved' || v === 'passed' || v === 'pass') {
    return 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-400';
  }
  if (
    v === 'rejected' ||
    v === 'gaps_found' ||
    v === 'changes_requested' ||
    v === 'fail'
  ) {
    return 'bg-rose-500/15 text-rose-700 dark:text-rose-400';
  }
  return 'bg-muted text-muted-foreground';
}

function GateRow({ d }: { d: GateDecisionOut }) {
  const gateLabel = GATE_LABEL[d.gate] ?? d.gate;
  const sourceLabel = SOURCE_LABEL[d.source] ?? d.source;
  const sourceTone = SOURCE_TONE[d.source] ?? 'bg-muted text-muted-foreground';
  return (
    <div className="border-l-2 pl-3 py-1.5">
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium">
          {gateLabel}
        </span>
        <span
          className={cn(
            'rounded px-1.5 py-0.5 text-[10px] font-medium',
            sourceTone,
          )}
        >
          {sourceLabel}
        </span>
        <span
          className={cn(
            'rounded px-1.5 py-0.5 text-[10px] font-medium uppercase',
            verdictTone(d.verdict),
          )}
        >
          {d.verdict || '—'}
        </span>
        {d.agent_id && (
          <code className="text-[10px] text-muted-foreground">{d.agent_id}</code>
        )}
        <span className="ml-auto text-[10px] text-muted-foreground">
          {new Date(d.created_at).toLocaleString()}
        </span>
      </div>
      {d.comments && (
        <div className="mt-1 whitespace-pre-wrap break-words rounded bg-muted/40 px-2 py-1 text-[11px]">
          {d.comments}
        </div>
      )}
      {d.cited_context && d.cited_context.length > 0 && (
        <div className="mt-1 text-[11px] text-muted-foreground">
          <span className="font-medium">Cited:</span> {d.cited_context.join(', ')}
        </div>
      )}
      {d.fallback_reasons && d.fallback_reasons.length > 0 && (
        <div className="mt-1 text-[11px] text-amber-700 dark:text-amber-400">
          <span className="font-medium">Fallback:</span>{' '}
          {d.fallback_reasons.join(', ')}
        </div>
      )}
    </div>
  );
}

export function GateHistoryPanel({ taskId }: { taskId: number }) {
  const [open, setOpen] = useState(true);
  const { data = [], isLoading } = useGateHistory(taskId);

  if (isLoading) {
    return <div className="text-xs text-muted-foreground">Loading gate history…</div>;
  }
  if (data.length === 0) {
    return (
      <div className="text-xs text-muted-foreground">No gate decisions yet.</div>
    );
  }

  return (
    <div className="rounded border bg-muted/30">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1 px-2 py-1.5 text-left text-xs font-medium hover:bg-muted"
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span>Gate history ({data.length})</span>
      </button>
      <div className={cn('space-y-1 px-3 pb-3 text-xs', !open && 'hidden')}>
        {data.map((d) => (
          <GateRow key={d.id} d={d} />
        ))}
      </div>
    </div>
  );
}
