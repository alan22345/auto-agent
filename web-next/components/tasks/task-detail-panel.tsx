'use client';
import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { TaskData } from '@/types/api';
import { cn } from '@/lib/utils';
import { AttemptsPanel } from './attempts-panel';
import { ScaffoldPhaseBanner } from './scaffold/scaffold-phase-banner';
import { IntentGrillCard } from './scaffold/intent-grill-card';
import { DomainGrillCard } from './scaffold/domain-grill-card';
import { RootAdrReviewCard } from './scaffold/root-adr-review-card';
import { DomainAdrReviewList } from './scaffold/domain-adr-review-list';
import { useScaffoldInvalidationOnWS } from '@/hooks/useScaffoldArtefacts';
import { useTasks } from '@/hooks/useTasks';

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
const ATTEMPTS_VISIBLE_STATUSES = new Set([
  'verifying',
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

// ADR-018 — child tasks of a SCAFFOLD parent run the existing trio
// flow. We surface them as a flat list rather than reusing TaskList
// (which is full-width with selection state) so they fit inside the
// detail panel for the scaffold parent.
function ScaffoldChildrenList({ parentId }: { parentId: number }) {
  const { data: tasks = [] } = useTasks();
  const children = tasks.filter((t) => t.parent_task_id === parentId);
  if (children.length === 0) {
    return (
      <div className="rounded border bg-muted/30 p-3 text-xs text-muted-foreground">
        No child trios yet.
      </div>
    );
  }
  return (
    <div className="rounded border bg-muted/30">
      <div className="border-b px-3 py-2 text-sm font-medium">
        Domain trios ({children.length})
      </div>
      <ul className="divide-y">
        {children.map((c) => (
          <li
            key={c.id}
            className="flex items-center gap-2 px-3 py-1.5 text-xs"
          >
            <span className="font-mono text-[10px] text-muted-foreground">
              #{c.id}
            </span>
            <span className="flex-1 truncate">{c.title || `Task ${c.id}`}</span>
            <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
              {c.status}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ADR-018 — SCAFFOLD parents drive a separate state machine; this set
// covers every status that means "this task is a scaffold parent" so we
// can render the scaffold banner + the relevant gate card.
const SCAFFOLD_STATUSES = new Set([
  'awaiting_intent_grill',
  'building_root_adr',
  'awaiting_root_adr_approval',
  'building_domain_adrs',
  'awaiting_domain_grill',
  'awaiting_domain_adr_approval',
  'dispatching_domain_builds',
  'building_domains',
  'awaiting_final_verification',
]);

export function TaskDetailPanel({ task }: { task: TaskData }) {
  const [planOpen, setPlanOpen] = useState(false);

  // Invalidate scaffold queries on WS events so the panel reflects new
  // verdicts / phase advances without a manual refresh. This is a no-op
  // for non-scaffold tasks (the hook just watches the wire).
  useScaffoldInvalidationOnWS(task.id);

  const isScaffold =
    task.complexity === 'scaffold' || SCAFFOLD_STATUSES.has(task.status);

  const hasDescription = !!task.description?.trim();
  const showPlan = shouldShowPanelPlan(task);
  const showError = !!task.error?.trim() && ERROR_VISIBLE_STATUSES.has(task.status);
  const showAttempts = ATTEMPTS_VISIBLE_STATUSES.has(task.status);

  if (
    !hasDescription &&
    !showPlan &&
    !showError &&
    !showAttempts &&
    !isScaffold
  )
    return null;

  return (
    <div className="space-y-2 border-b px-4 py-3">
      {hasDescription && (
        <div className="max-h-64 overflow-y-auto whitespace-pre-wrap text-xs leading-relaxed text-muted-foreground">
          {task.description}
        </div>
      )}

      {isScaffold && (
        <>
          <ScaffoldPhaseBanner status={task.status} />
          {task.status === 'awaiting_intent_grill' && (
            <IntentGrillCard taskId={task.id} />
          )}
          {task.status === 'awaiting_root_adr_approval' && (
            <RootAdrReviewCard taskId={task.id} />
          )}
          {task.status === 'awaiting_domain_grill' && (
            <DomainGrillCard taskId={task.id} />
          )}
          {task.status === 'awaiting_domain_adr_approval' && (
            <DomainAdrReviewList taskId={task.id} />
          )}
          {(task.status === 'building_domains' ||
            task.status === 'dispatching_domain_builds' ||
            task.status === 'awaiting_final_verification') && (
            <ScaffoldChildrenList parentId={task.id} />
          )}
        </>
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

      {showAttempts && <AttemptsPanel taskId={task.id} />}
    </div>
  );
}
