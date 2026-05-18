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
import { ScaffoldPhaseBanner } from './scaffold/scaffold-phase-banner';
import { IntentGrillCard } from './scaffold/intent-grill-card';
import { DomainGrillCard } from './scaffold/domain-grill-card';
import { RootAdrReviewCard } from './scaffold/root-adr-review-card';
import { DomainAdrReviewList } from './scaffold/domain-adr-review-list';
import { useScaffoldInvalidationOnWS } from '@/hooks/useScaffoldArtefacts';
import { useTasks } from '@/hooks/useTasks';

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
    !isScaffold &&
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

      {showApprovalGate && !isScaffold && <PlanApprovalCard taskId={task.id} />}

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
