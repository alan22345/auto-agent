// URL <-> focus path encoding for the capability/flow map (Phase 5 §6).
//
// ``p=<capability_id>/<flow_id>/<step_node_id>`` with empty segments
// allowed (e.g. ``p=cap_0`` for LOD 1, ``p=cap_0/flow_a`` for LOD 2).
// Step node ids may contain ``/`` (file paths) so the step segment is
// percent-encoded as a single unit.
//
// Kept out of ``app/(app)/code-graph/[repoId]/page.tsx`` because
// Next.js disallows arbitrary named exports from a page file
// (the production build's typecheck enforces this).

import { ROOT_FOCUS, type FocusPath } from '@/components/code-graph/map-canvas';

export function encodeFocusForQuery(focus: FocusPath): string | null {
  if (!focus.capabilityId) return null;
  const segments = [focus.capabilityId];
  if (focus.flowId) {
    segments.push(focus.flowId);
    if (focus.stepNodeId) {
      segments.push(encodeURIComponent(focus.stepNodeId));
    }
  }
  return segments.join('/');
}

export function parseFocusFromQuery(raw: string | null): FocusPath {
  if (!raw) return ROOT_FOCUS;
  const [cap, flow, step] = raw.split('/', 3);
  return {
    capabilityId: cap || null,
    flowId: flow || null,
    stepNodeId: step ? decodeURIComponent(step) : null,
  };
}

export function drillOut(focus: FocusPath): FocusPath {
  if (focus.stepNodeId) {
    return {
      capabilityId: focus.capabilityId,
      flowId: focus.flowId,
      stepNodeId: null,
    };
  }
  if (focus.flowId) {
    return {
      capabilityId: focus.capabilityId,
      flowId: null,
      stepNodeId: null,
    };
  }
  return ROOT_FOCUS;
}
