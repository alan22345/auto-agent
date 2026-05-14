'use client';
import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { TaskData } from '@/types/api';
import { cn } from '@/lib/utils';
import { AttemptsPanel } from './attempts-panel';
import { PlanApprovalCard } from './plan-approval-card';
import { ArchitectAttemptsPanel } from '@/components/trio/ArchitectAttemptsPanel';
import { GateHistoryPanel } from '@/components/trio/GateHistoryPanel';
import { TrioReviewAttemptsPanel } from '@/components/trio/TrioReviewAttemptsPanel';
import { DecisionsPanel } from '@/components/trio/DecisionsPanel';
import { PauseTrioButton } from '@/components/trio/PauseTrioButton';

// ADR-015 §2 Phase 12 — both plan and design gates are surfaced via the
// shared PlanApprovalCard (the design doc IS the single approval
// artefact per §2). The legacy plan accordion only renders for statuses
// that don't have an active approval gate.
const APPROVAL_GATE_STATUSES = new Set([
  'awaiting_plan_approval',
  'awaiting_design_approval',
]);

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

export function TaskDetailPanel({ task }: { task: TaskData }) {
  const [planOpen, setPlanOpen] = useState(false);

  const hasDescription = !!task.description?.trim();
  const showApprovalGate = APPROVAL_GATE_STATUSES.has(task.status);
  // The legacy plan accordion is hidden while the approval gate is
  // active — the PlanApprovalCard owns the markdown + actions then.
  const showPlan = !showApprovalGate && shouldShowPanelPlan(task);
  const showError = !!task.error?.trim() && ERROR_VISIBLE_STATUSES.has(task.status);
  const showAttempts = ATTEMPTS_VISIBLE_STATUSES.has(task.status);
  const isTrioParent = task.status === 'trio_executing' || !!task.trio_phase;
  const isTrioChild = !!task.parent_task_id;
  // Gate-history panel is visible whenever the task has progressed past
  // intake — i.e., there could plausibly be a decision to render. The
  // panel itself renders nothing when the list is empty.
  const showGateHistory =
    !!task.status &&
    !['intake', 'classifying', 'queued'].includes(task.status);

  if (
    !hasDescription &&
    !showApprovalGate &&
    !showPlan &&
    !showError &&
    !showAttempts &&
    !isTrioParent &&
    !isTrioChild &&
    !showGateHistory
  )
    return null;

  return (
    <div className="space-y-2 border-b px-4 py-3">
      {hasDescription && (
        <div className="max-h-64 overflow-y-auto whitespace-pre-wrap text-xs leading-relaxed text-muted-foreground">
          {task.description}
        </div>
      )}

      {showApprovalGate && <PlanApprovalCard taskId={task.id} />}

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

      {isTrioParent && (
        <section className="space-y-4 pt-2">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">Trio</h2>
            <PauseTrioButton taskId={task.id} />
          </div>
          <div className="text-xs text-muted-foreground">
            Phase: <strong>{task.trio_phase ?? '—'}</strong>
            {task.trio_backlog && (
              <> · Backlog: {task.trio_backlog.filter((w) => (w as { status?: string }).status === 'done').length} / {task.trio_backlog.length} done</>
            )}
          </div>
          <h3 className="text-xs font-medium">Architect activity</h3>
          <ArchitectAttemptsPanel taskId={task.id} />
          <h3 className="text-xs font-medium">Decisions (ADRs)</h3>
          <DecisionsPanel taskId={task.id} />
        </section>
      )}

      {isTrioChild && (
        <section className="space-y-2 pt-2">
          <h2 className="text-sm font-semibold">Trio Reviews</h2>
          <TrioReviewAttemptsPanel taskId={task.id} />
        </section>
      )}

      {showGateHistory && (
        <section className="space-y-2 pt-2">
          <GateHistoryPanel taskId={task.id} />
        </section>
      )}
    </div>
  );
}
