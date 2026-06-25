// Boundary-port computation for the capability/flow map (Phase 4).
//
// Spec §5:
//
// > When focused at LOD 1 inside one capability, edges to steps that
// > live in *another* capability render as ports labelled
// > ``→ <capability_name>``. Clicking drills out one LOD and pans to
// > the destination.
//
// > When focused at LOD 2 on one flow, calls into steps that are also
// > part of *other* flows in the same capability render as ports on
// > the right edge of the flow tile, labelled
// > ``→ also used in: <flow_name>, <flow_name>``. Clicking the port
// > pans/drills to the sibling flow.
//
// In a pure-DOM realisation the "right-edge ports" become a stable
// bottom-of-tile / side panel that lists the linked capability or sibling
// flow. The data shape stays the same.

import type { Capability, Flow, FlowJsonBlob } from '@/types/api';

export interface CapabilityPort {
  capabilityId: string;
  capabilityName: string;
  /** node ids in this capability's flows that link out. Helpful when
   * the port summary needs counts ("3 calls to Auth"). */
  via: string[];
}

export interface FlowPort {
  flowId: string;
  flowName: string;
  /** the step ``node_id`` shared between the focused flow and this
   * sibling flow. */
  sharedNodeId: string;
}

interface NodeToCapability {
  /** Node id → capability id index (built from the labelled blob). A
   * node belongs to a capability if any step on a flow in that
   * capability references it. Nodes appearing in multiple capabilities
   * resolve to the *first* one in iteration order — by construction
   * the labeller produces a partition, but defensive handling avoids
   * crashing if a future shape drift breaks the invariant. */
  byNode: Map<string, string>;
}

function indexNodeToCapability(blob: FlowJsonBlob): NodeToCapability {
  const flowsById = new Map<string, Flow>(blob.flows.map((f) => [f.id, f]));
  const byNode = new Map<string, string>();
  for (const cap of blob.capabilities) {
    for (const fid of cap.flow_ids) {
      const f = flowsById.get(fid);
      if (!f) continue;
      for (const step of f.steps) {
        if (!byNode.has(step.node_id)) byNode.set(step.node_id, cap.id);
      }
    }
  }
  return { byNode };
}

// LOD 1 ports — outbound capabilities linked from the focused
// capability's flow steps. The labels read off ``capability.name``
// when available, falling back to ``capability.id``.
export function computeCapabilityPorts(
  blob: FlowJsonBlob,
  focusedCapability: Capability,
): CapabilityPort[] {
  const flowsById = new Map<string, Flow>(blob.flows.map((f) => [f.id, f]));
  const capsById = new Map<string, Capability>(
    blob.capabilities.map((c) => [c.id, c]),
  );
  const index = indexNodeToCapability(blob);
  const ports = new Map<string, CapabilityPort>();
  for (const fid of focusedCapability.flow_ids) {
    const flow = flowsById.get(fid);
    if (!flow) continue;
    for (const step of flow.steps) {
      const owner = index.byNode.get(step.node_id);
      if (!owner || owner === focusedCapability.id) continue;
      const target = capsById.get(owner);
      const port = ports.get(owner) ?? {
        capabilityId: owner,
        capabilityName: target?.name ?? owner,
        via: [],
      };
      port.via.push(step.node_id);
      ports.set(owner, port);
    }
  }
  return Array.from(ports.values()).sort((a, b) =>
    a.capabilityName.localeCompare(b.capabilityName),
  );
}

// LOD 2 ports — sibling flows in the same capability that share a step
// with the focused flow. Useful for spotting that a util function is
// invoked by multiple flows of the same capability.
export function computeSiblingFlowPorts(
  blob: FlowJsonBlob,
  focusedCapability: Capability,
  focusedFlow: Flow,
): FlowPort[] {
  const flowsById = new Map<string, Flow>(blob.flows.map((f) => [f.id, f]));
  const focusedNodeIds = new Set(focusedFlow.steps.map((s) => s.node_id));
  const ports: FlowPort[] = [];
  for (const fid of focusedCapability.flow_ids) {
    if (fid === focusedFlow.id) continue;
    const sibling = flowsById.get(fid);
    if (!sibling) continue;
    for (const step of sibling.steps) {
      if (!focusedNodeIds.has(step.node_id)) continue;
      ports.push({
        flowId: sibling.id,
        flowName: sibling.name ?? sibling.id,
        sharedNodeId: step.node_id,
      });
      break; // one port per sibling flow keeps the summary compact
    }
  }
  return ports.sort((a, b) => a.flowName.localeCompare(b.flowName));
}
