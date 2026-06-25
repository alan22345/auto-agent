import { describe, it, expect } from 'vitest';
import {
  encodeFocusForQuery,
  parseFocusFromQuery,
} from '@/lib/code-graph-focus';
import {
  ROOT_FOCUS,
  type FocusPath,
} from '@/components/code-graph/map-canvas';
import {
  computeCapabilityPorts,
  computeSiblingFlowPorts,
} from '@/components/code-graph/map-boundary-ports';
import type { Capability, Flow, FlowJsonBlob } from '@/types/api';

const makeFlow = (overrides: Partial<Flow>): Flow => ({
  id: overrides.id ?? 'flow_x',
  entry_point: overrides.entry_point ?? {
    node_id: 'app:entry',
    kind: 'http',
  },
  terminal_node_id: overrides.terminal_node_id ?? 'db:write',
  terminal_kind: overrides.terminal_kind ?? 'db_write',
  steps: overrides.steps ?? [
    { node_id: 'app:entry', depth: 0, is_branch_root: false, is_cycle_back: false },
  ],
  file_set: overrides.file_set ?? ['app.py'],
  file_set_hash: overrides.file_set_hash ?? 'sha256:1',
  name: overrides.name ?? null,
  description: overrides.description ?? null,
  labeled_at_commit: overrides.labeled_at_commit ?? null,
});

const makeCap = (
  id: string,
  name: string | null,
  flow_ids: string[],
): Capability => ({
  id,
  flow_ids,
  flow_membership_hash: 'sha256:c',
  name,
  description: null,
  labeled_at_commit: null,
});

describe('encodeFocusForQuery / parseFocusFromQuery', () => {
  it('encodes null capability as null (omits the param)', () => {
    expect(encodeFocusForQuery(ROOT_FOCUS)).toBeNull();
  });
  it('round-trips capability-only', () => {
    const f: FocusPath = { capabilityId: 'cap_0', flowId: null, stepNodeId: null };
    const encoded = encodeFocusForQuery(f);
    expect(encoded).toBe('cap_0');
    expect(parseFocusFromQuery(encoded)).toEqual(f);
  });
  it('round-trips capability + flow', () => {
    const f: FocusPath = {
      capabilityId: 'cap_0',
      flowId: 'flow_a',
      stepNodeId: null,
    };
    const encoded = encodeFocusForQuery(f);
    expect(encoded).toBe('cap_0/flow_a');
    expect(parseFocusFromQuery(encoded)).toEqual(f);
  });
  it('percent-encodes step node ids containing /', () => {
    const f: FocusPath = {
      capabilityId: 'cap_0',
      flowId: 'flow_a',
      stepNodeId: 'app/auth/login.py::login',
    };
    const encoded = encodeFocusForQuery(f);
    // The step segment must not contain raw '/' or the parser would
    // mis-split into four segments.
    expect(encoded).not.toBeNull();
    const stepSegment = (encoded as string).split('/').slice(2).join('/');
    expect(stepSegment).not.toContain('app/auth');
    expect(parseFocusFromQuery(encoded)).toEqual(f);
  });
  it('returns ROOT_FOCUS for null/empty', () => {
    expect(parseFocusFromQuery(null)).toEqual(ROOT_FOCUS);
    expect(parseFocusFromQuery('')).toEqual(ROOT_FOCUS);
  });
});

describe('computeCapabilityPorts', () => {
  // Two capabilities. Auth has a flow that touches a node owned by Carbon.
  const blob: FlowJsonBlob = {
    capabilities: [
      makeCap('cap_auth', 'Authentication', ['flow_login']),
      makeCap('cap_carbon', 'Carbon Calc', ['flow_calc']),
    ],
    flows: [
      makeFlow({
        id: 'flow_login',
        steps: [
          { node_id: 'app:login', depth: 0, is_branch_root: false, is_cycle_back: false },
          { node_id: 'lib:hash', depth: 1, is_branch_root: false, is_cycle_back: false },
          // This step is also in flow_calc → it belongs to cap_carbon.
          { node_id: 'lib:carbon_util', depth: 2, is_branch_root: false, is_cycle_back: false },
        ],
      }),
      makeFlow({
        id: 'flow_calc',
        steps: [
          { node_id: 'app:calc', depth: 0, is_branch_root: false, is_cycle_back: false },
          { node_id: 'lib:carbon_util', depth: 1, is_branch_root: false, is_cycle_back: false },
        ],
      }),
    ],
    unreached: [],
    derived_at_commit: 'abc',
    deriver_version: 'phase1',
    labeler_model: null,
  };

  it('computeCapabilityPorts surfaces cross-capability links', () => {
    // Build a blob where the order is reversed so flow_calc indexes
    // lib:carbon_util first → owner=cap_carbon → flow_login sees a
    // capability port to cap_carbon.
    const reorder: FlowJsonBlob = {
      ...blob,
      capabilities: [
        makeCap('cap_carbon', 'Carbon Calc', ['flow_calc']),
        makeCap('cap_auth', 'Authentication', ['flow_login']),
      ],
    };
    const ports = computeCapabilityPorts(
      reorder,
      reorder.capabilities[1]!, // cap_auth
    );
    expect(ports).toHaveLength(1);
    expect(ports[0]).toMatchObject({
      capabilityId: 'cap_carbon',
      capabilityName: 'Carbon Calc',
    });
    expect(ports[0]!.via).toContain('lib:carbon_util');
  });

  it('returns empty when no cross-capability calls exist', () => {
    const isolated: FlowJsonBlob = {
      ...blob,
      flows: [
        makeFlow({
          id: 'flow_login',
          steps: [
            { node_id: 'app:login', depth: 0, is_branch_root: false, is_cycle_back: false },
          ],
        }),
      ],
      capabilities: [makeCap('cap_auth', 'Authentication', ['flow_login'])],
    };
    expect(
      computeCapabilityPorts(isolated, isolated.capabilities[0]!),
    ).toHaveLength(0);
  });
});

describe('computeSiblingFlowPorts', () => {
  // One capability, two flows sharing a step.
  const blob: FlowJsonBlob = {
    capabilities: [
      makeCap('cap_auth', 'Authentication', ['flow_login', 'flow_oauth']),
    ],
    flows: [
      makeFlow({
        id: 'flow_login',
        name: 'Email Login',
        steps: [
          { node_id: 'app:login', depth: 0, is_branch_root: false, is_cycle_back: false },
          { node_id: 'lib:hash', depth: 1, is_branch_root: false, is_cycle_back: false },
          { node_id: 'lib:sessions', depth: 2, is_branch_root: false, is_cycle_back: false },
        ],
      }),
      makeFlow({
        id: 'flow_oauth',
        name: 'OAuth Login',
        steps: [
          { node_id: 'app:oauth', depth: 0, is_branch_root: false, is_cycle_back: false },
          // shares lib:sessions with flow_login
          { node_id: 'lib:sessions', depth: 1, is_branch_root: false, is_cycle_back: false },
        ],
      }),
    ],
    unreached: [],
    derived_at_commit: 'abc',
    deriver_version: 'phase1',
    labeler_model: null,
  };

  it('surfaces sibling flows that share a step', () => {
    const focusedFlow = blob.flows[0]!;
    const ports = computeSiblingFlowPorts(
      blob,
      blob.capabilities[0]!,
      focusedFlow,
    );
    expect(ports).toHaveLength(1);
    expect(ports[0]).toMatchObject({
      flowId: 'flow_oauth',
      flowName: 'OAuth Login',
      sharedNodeId: 'lib:sessions',
    });
  });

  it('skips the focused flow itself', () => {
    const focusedFlow = blob.flows[0]!;
    const ports = computeSiblingFlowPorts(
      blob,
      blob.capabilities[0]!,
      focusedFlow,
    );
    for (const p of ports) {
      expect(p.flowId).not.toBe(focusedFlow.id);
    }
  });

  it('returns empty when no siblings share any node', () => {
    const isolated: FlowJsonBlob = {
      ...blob,
      flows: [
        makeFlow({
          id: 'flow_login',
          steps: [
            { node_id: 'app:login', depth: 0, is_branch_root: false, is_cycle_back: false },
          ],
        }),
        makeFlow({
          id: 'flow_oauth',
          steps: [
            { node_id: 'app:oauth', depth: 0, is_branch_root: false, is_cycle_back: false },
          ],
        }),
      ],
    };
    const ports = computeSiblingFlowPorts(
      isolated,
      isolated.capabilities[0]!,
      isolated.flows[0]!,
    );
    expect(ports).toHaveLength(0);
  });
});
