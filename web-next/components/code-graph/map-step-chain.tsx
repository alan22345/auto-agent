// LOD 2 — ordered chain of FlowSteps for the focused flow. Phase 4.
//
// Renders each step as a card, with arrows + branch/cycle indicators
// between cards. Click on a step drills into LOD 3 source preview.
//
// Branch nodes (``is_branch_root``) render with a fan-out caret.
// Cycle-back back-edges (``is_cycle_back``) render with the ↺ glyph.
'use client';
import {
  ArrowDown,
  GitBranch,
  RotateCcw,
  ExternalLink,
} from 'lucide-react';
import type { Flow, FlowStep, Node as GraphNode } from '@/types/api';

interface Props {
  flow: Flow;
  nodesById: Map<string, GraphNode>;
  onOpenStep: (nodeId: string) => void;
}

export function StepChain({ flow, nodesById, onOpenStep }: Props) {
  return (
    <ol
      data-testid="step-chain"
      className="flex flex-col items-stretch gap-1 p-4"
    >
      {flow.steps.map((step, i) => (
        <li key={`${step.node_id}@${i}`} className="flex flex-col items-stretch">
          <StepCard
            step={step}
            node={nodesById.get(step.node_id) ?? null}
            onOpen={() => onOpenStep(step.node_id)}
          />
          {i < flow.steps.length - 1 && (
            <ConnectorBetween from={step} to={flow.steps[i + 1]} />
          )}
        </li>
      ))}
    </ol>
  );
}

function StepCard({
  step,
  node,
  onOpen,
}: {
  step: FlowStep;
  node: GraphNode | null;
  onOpen: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onOpen}
      onDoubleClick={onOpen}
      data-testid={`step-card-${step.node_id}`}
      data-branch={step.is_branch_root ? 'true' : undefined}
      data-cycle-back={step.is_cycle_back ? 'true' : undefined}
      className="group flex items-center gap-3 rounded-md border bg-card px-3 py-2 text-left transition hover:border-primary hover:bg-card/80 focus:outline-none focus:ring-2 focus:ring-primary"
    >
      <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
        d{step.depth}
      </span>
      <span className="min-w-0 flex-1 truncate text-xs font-medium">
        {node?.label ?? step.node_id}
      </span>
      {node?.file && (
        <span className="hidden truncate font-mono text-[10px] text-muted-foreground sm:inline">
          {node.file}:{node.line_start}
        </span>
      )}
      {step.is_branch_root && (
        <GitBranch
          size={12}
          className="shrink-0 text-amber-500"
          aria-label="branches"
        />
      )}
      {step.is_cycle_back && (
        <RotateCcw
          size={12}
          className="shrink-0 text-purple-500"
          aria-label="cycle back-edge"
        />
      )}
      <ExternalLink
        size={11}
        className="shrink-0 text-muted-foreground opacity-0 group-hover:opacity-100"
        aria-hidden
      />
    </button>
  );
}

function ConnectorBetween({ from, to }: { from: FlowStep; to: FlowStep }) {
  // Visual indicator on the connector when the next step represents a
  // branch fork (from.is_branch_root) or a cycle back-edge target
  // (to.is_cycle_back).
  if (to.is_cycle_back) {
    return (
      <div
        aria-hidden
        className="flex items-center justify-center py-0.5 text-purple-500"
      >
        <RotateCcw size={12} />
      </div>
    );
  }
  if (from.is_branch_root) {
    return (
      <div
        aria-hidden
        className="flex items-center justify-center py-0.5 text-amber-500"
      >
        <GitBranch size={12} />
      </div>
    );
  }
  return (
    <div
      aria-hidden
      className="flex items-center justify-center py-0.5 text-muted-foreground"
    >
      <ArrowDown size={12} />
    </div>
  );
}
