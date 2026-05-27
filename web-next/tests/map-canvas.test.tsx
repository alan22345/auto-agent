/** @vitest-environment jsdom */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  MapCanvas,
  ROOT_FOCUS,
  type FocusPath,
} from '@/components/code-graph/map-canvas';
import type { FlowJsonBlob, RepoGraphBlob } from '@/types/api';

const FLOW_BLOB: FlowJsonBlob = {
  capabilities: [
    {
      id: 'cap_auth',
      flow_ids: ['flow_login'],
      flow_membership_hash: 'sha256:1',
      name: 'Authentication',
      description: 'Login + session',
      labeled_at_commit: null,
    },
    {
      id: 'cap_carbon',
      flow_ids: ['flow_calc'],
      flow_membership_hash: 'sha256:2',
      name: 'Carbon Calc',
      description: null,
      labeled_at_commit: null,
    },
  ],
  flows: [
    {
      id: 'flow_login',
      entry_point: { node_id: 'app:login', kind: 'http' },
      terminal_node_id: 'lib:sessions',
      terminal_kind: 'db_write',
      steps: [
        { node_id: 'app:login', depth: 0, is_branch_root: false, is_cycle_back: false },
        { node_id: 'lib:hash', depth: 1, is_branch_root: true, is_cycle_back: false },
        { node_id: 'lib:sessions', depth: 2, is_branch_root: false, is_cycle_back: false },
      ],
      file_set: ['app.py', 'lib.py'],
      file_set_hash: 'sha256:f',
      name: 'Email Login',
      description: 'Validates credentials',
      labeled_at_commit: null,
    },
    {
      id: 'flow_calc',
      entry_point: { node_id: 'app:calc', kind: 'http' },
      terminal_node_id: 'db:write',
      terminal_kind: 'db_write',
      steps: [
        { node_id: 'app:calc', depth: 0, is_branch_root: false, is_cycle_back: false },
      ],
      file_set: ['calc.py'],
      file_set_hash: 'sha256:c',
      name: 'Carbon Calc',
      description: null,
      labeled_at_commit: null,
    },
  ],
  unreached: ['lib:noop'],
  derived_at_commit: 'abc',
  deriver_version: 'phase1',
  labeler_model: 'claude-haiku-4-5',
};

const GRAPH_BLOB: RepoGraphBlob = {
  commit_sha: 'abc',
  generated_at: '2026-05-22T00:00:00Z',
  analyser_version: 'phase2-python-0.2.0',
  areas: [{ name: 'app', status: 'ok', error: null, unresolved_dynamic_sites: 0 }],
  nodes: [
    {
      id: 'app:login',
      kind: 'function',
      label: 'login',
      file: 'app.py',
      line_start: 1,
      line_end: 8,
      area: 'app',
      parent: null,
    },
    {
      id: 'lib:hash',
      kind: 'function',
      label: 'hash_password',
      file: 'lib.py',
      line_start: 10,
      line_end: 14,
      area: 'app',
      parent: null,
    },
    {
      id: 'lib:sessions',
      kind: 'function',
      label: 'create_session',
      file: 'lib.py',
      line_start: 20,
      line_end: 30,
      area: 'app',
      parent: null,
    },
  ],
  edges: [],
};

function wrap(focus: FocusPath = ROOT_FOCUS) {
  const onFocusChange = vi.fn();
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const utils = render(
    <QueryClientProvider client={qc}>
      <MapCanvas
        blob={FLOW_BLOB}
        focus={focus}
        onFocusChange={onFocusChange}
        repoId={1}
        graphBlob={GRAPH_BLOB}
      />
    </QueryClientProvider>,
  );
  return { ...utils, onFocusChange };
}

describe('MapCanvas LOD 0', () => {
  it('renders one tile per capability', () => {
    wrap();
    expect(screen.getByTestId('capability-tile-cap_auth')).toBeTruthy();
    expect(screen.getByTestId('capability-tile-cap_carbon')).toBeTruthy();
  });

  it('drills into a capability when its tile is clicked', () => {
    const { onFocusChange } = wrap();
    fireEvent.click(screen.getByTestId('capability-tile-cap_auth'));
    expect(onFocusChange).toHaveBeenCalledWith({
      capabilityId: 'cap_auth',
      flowId: null,
      stepNodeId: null,
    });
  });

  it('renders the Unreached tray with the count', () => {
    wrap();
    const tray = screen.getByTestId('unreached-tray');
    expect(tray.textContent).toContain('Unreached');
    expect(tray.textContent).toContain('1');
  });

  it('filters tiles by search query', () => {
    const onFocusChange = vi.fn();
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={qc}>
        <MapCanvas
          blob={FLOW_BLOB}
          focus={ROOT_FOCUS}
          onFocusChange={onFocusChange}
          searchQuery="auth"
          repoId={1}
          graphBlob={GRAPH_BLOB}
        />
      </QueryClientProvider>,
    );
    expect(screen.queryByTestId('capability-tile-cap_auth')).not.toBeNull();
    expect(screen.queryByTestId('capability-tile-cap_carbon')).toBeNull();
  });
});

describe('MapCanvas LOD 1', () => {
  it('renders one tile per flow in the focused capability', () => {
    wrap({ capabilityId: 'cap_auth', flowId: null, stepNodeId: null });
    expect(screen.getByTestId('flow-tile-flow_login')).toBeTruthy();
  });

  it('drills into a flow when its tile is clicked', () => {
    const { onFocusChange } = wrap({
      capabilityId: 'cap_auth',
      flowId: null,
      stepNodeId: null,
    });
    fireEvent.click(screen.getByTestId('flow-tile-flow_login'));
    expect(onFocusChange).toHaveBeenCalledWith({
      capabilityId: 'cap_auth',
      flowId: 'flow_login',
      stepNodeId: null,
    });
  });

  it('breadcrumb shows the capability segment', () => {
    wrap({ capabilityId: 'cap_auth', flowId: null, stepNodeId: null });
    const breadcrumb = screen.getByTestId('map-breadcrumb');
    expect(breadcrumb.textContent).toContain('Capabilities');
    expect(breadcrumb.textContent).toContain('Authentication');
  });
});

describe('MapCanvas LOD 2', () => {
  it('renders one step card per flow step with labels from the graph blob', () => {
    wrap({
      capabilityId: 'cap_auth',
      flowId: 'flow_login',
      stepNodeId: null,
    });
    const chain = screen.getByTestId('step-chain');
    expect(chain.textContent).toContain('login');
    expect(chain.textContent).toContain('hash_password');
    expect(chain.textContent).toContain('create_session');
  });

  it('marks branch-root steps with the branch indicator', () => {
    wrap({
      capabilityId: 'cap_auth',
      flowId: 'flow_login',
      stepNodeId: null,
    });
    const branchCard = screen.getByTestId('step-card-lib:hash');
    expect(branchCard.getAttribute('data-branch')).toBe('true');
  });

  it('drills into LOD 3 when a step card is clicked', () => {
    const { onFocusChange } = wrap({
      capabilityId: 'cap_auth',
      flowId: 'flow_login',
      stepNodeId: null,
    });
    fireEvent.click(screen.getByTestId('step-card-app:login'));
    expect(onFocusChange).toHaveBeenCalledWith({
      capabilityId: 'cap_auth',
      flowId: 'flow_login',
      stepNodeId: 'app:login',
    });
  });
});

describe('MapCanvas LOD 3', () => {
  it('renders the source preview panel anchored to the focused step', () => {
    wrap({
      capabilityId: 'cap_auth',
      flowId: 'flow_login',
      stepNodeId: 'app:login',
    });
    // The fetch-driven preview won't resolve in jsdom without a stub,
    // but the wrapper renders the header from node data immediately.
    const panel = screen.getByTestId('map-source-preview');
    expect(panel.textContent).toContain('login');
    expect(panel.textContent).toContain('app.py');
  });
});
