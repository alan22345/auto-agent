'use client';
import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { TaskData } from '@/types/api';
import { cn } from '@/lib/utils';

const PLAN_VISIBLE_STATUSES = new Set([
  'awaiting_approval',
  'awaiting_clarification',
  'coding',
  'pr_created',
  'awaiting_ci',
  'awaiting_review',
  'done',
  'failed',
  'blocked',
]);
const ERROR_VISIBLE_STATUSES = new Set(['failed', 'blocked']);

function shouldShowPanelPlan(task: TaskData): boolean {
  return !!(
    task.plan &&
    task.plan.trim() &&
    task.complexity !== 'simple' &&
    task.complexity !== 'simple_no_code' &&
    PLAN_VISIBLE_STATUSES.has(task.status)
  );
}

// Strip thinking/exploration preamble — start at first markdown heading.
function getDisplayablePlanText(plan: string): string {
  const m = plan.match(/^(#{1,3} )/m);
  if (m) return plan.substring(plan.indexOf(m[0]));
  return plan;
}

export function TaskDetailPanel({ task }: { task: TaskData }) {
  const [planOpen, setPlanOpen] = useState(false);

  const hasDescription = !!task.description?.trim();
  const showPlan = shouldShowPanelPlan(task);
  const showError = !!task.error?.trim() && ERROR_VISIBLE_STATUSES.has(task.status);

  if (!hasDescription && !showPlan && !showError) return null;

  return (
    <div className="space-y-2 border-b px-4 py-3">
      {hasDescription && (
        <div className="max-h-64 overflow-y-auto whitespace-pre-wrap text-xs leading-relaxed text-muted-foreground">
          {task.description}
        </div>
      )}

      {showPlan && (
        <div className="rounded border bg-muted/30">
          <button
            type="button"
            onClick={() => setPlanOpen((v) => !v)}
            className="flex w-full items-center gap-1 px-2 py-1.5 text-left text-xs font-medium hover:bg-muted"
          >
            {planOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            <span>
              {task.status === 'awaiting_approval' ? 'Plan — Review and Approve' : 'Plan'}
            </span>
          </button>
          <div
            className={cn(
              'prose prose-sm max-h-[50vh] max-w-none overflow-y-auto px-3 pb-3 text-xs leading-relaxed',
              !planOpen && 'hidden',
            )}
          >
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {getDisplayablePlanText(task.plan!)}
            </ReactMarkdown>
          </div>
        </div>
      )}

      {showError && (
        <div className="whitespace-pre-wrap rounded border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive">
          {task.error}
        </div>
      )}
    </div>
  );
}
