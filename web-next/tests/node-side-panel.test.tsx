import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  NodeSidePanel,
  collectSubtreeNodeIds,
  groupEdgesByKind,
} from '@/components/code-graph/node-side-panel';
import type { Edge, RepoGraphBlob } from '@/types/api';

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const blob: RepoGraphBlob = {
  commit_sha: 'abc',
  generated_at: '2026-05-15T00:00:00Z',
  analyser_version: 'phase7-0.7.0',
  areas: [],
  nodes: [
    {
      id: 'agent/dog.py::Dog.bark',
      kind: 'function',
      label: 'Dog.bark',
      file: 'agent/dog.py',
      line_start: 10,
      line_end: 20,
      area: 'agent',
      parent: 'class:agent/dog.py::Dog',
    },
    {
      id: 'agent/dog.py::Dog.speak',
      kind: 'function',
      label: 'Dog.speak',
      file: 'agent/dog.py',
      line_start: 22,
      line_end: 30,
      area: 'agent',
      parent: 'class:agent/dog.py::Dog',
    },
    {
      id: 'agent/cat.py::Cat.meow',
      kind: 'function',
      label: 'Cat.meow',
      file: 'agent/cat.py',
      line_start: 5,
      line_end: 8,
      area: 'agent',
      parent: 'class:agent/cat.py::Cat',
    },
    {
      id: 'agent/base.py::Animal',
      kind: 'class',
      label: 'Animal',
      file: 'agent/base.py',
      line_start: 1,
      line_end: 30,
      area: 'agent',
      parent: 'file:agent/base.py',
    },
  ],
  edges: [
    // outgoing — calls
    {
      source: 'agent/dog.py::Dog.bark',
      target: 'agent/dog.py::Dog.speak',
      kind: 'calls',
      evidence: { file: 'agent/dog.py', line: 11, snippet: 'self.speak()' },
      source_kind: 'ast',
      boundary_violation: false,
    },
    // outgoing — inherits
    {
      source: 'agent/dog.py::Dog.bark',
      target: 'agent/base.py::Animal',
      kind: 'inherits',
      evidence: { file: 'agent/dog.py', line: 1, snippet: 'class Dog(Animal):' },
      source_kind: 'ast',
      boundary_violation: false,
    },
    // incoming — calls (from Cat.meow into Dog.bark)
    {
      source: 'agent/cat.py::Cat.meow',
      target: 'agent/dog.py::Dog.bark',
      kind: 'calls',
      evidence: { file: 'agent/cat.py', line: 7, snippet: 'bark()' },
      source_kind: 'ast',
      boundary_violation: false,
    },
    // unrelated edge — must not show up
    {
      source: 'agent/cat.py::Cat.meow',
      target: 'agent/base.py::Animal',
      kind: 'imports',
      evidence: { file: 'agent/cat.py', line: 1, snippet: 'from base import Animal' },
      source_kind: 'ast',
      boundary_violation: false,
    },
  ],
};

describe('collectSubtreeNodeIds', () => {
  // ADR-016 §11 — when the user selects a compound (area/file) node,
  // the side panel must surface edges that cross the subtree boundary,
  // not just edges whose endpoint id literally equals the compound's id
  // (there are none — edges connect leaf nodes underneath). The
  // subtree-id helper drives that aggregation.
  const compoundBlob: RepoGraphBlob = {
    commit_sha: 'x',
    generated_at: '2026-05-21T00:00:00Z',
    analyser_version: 'phase7-0.7.0',
    areas: [],
    nodes: [
      {
        id: 'area:dispatcher',
        kind: 'area',
        label: 'dispatcher',
        file: null,
        line_start: null,
        line_end: null,
        area: 'dispatcher',
        parent: null,
      },
      {
        id: 'file:dispatcher/router.py',
        kind: 'file',
        label: 'router.py',
        file: 'dispatcher/router.py',
        line_start: 1,
        line_end: 12,
        area: 'dispatcher',
        parent: 'area:dispatcher',
      },
      {
        id: 'dispatcher/router.py::dispatch',
        kind: 'function',
        label: 'dispatch',
        file: 'dispatcher/router.py',
        line_start: 6,
        line_end: 8,
        area: 'dispatcher',
        parent: 'file:dispatcher/router.py',
      },
      {
        id: 'area:handlers',
        kind: 'area',
        label: 'handlers',
        file: null,
        line_start: null,
        line_end: null,
        area: 'handlers',
        parent: null,
      },
      {
        id: 'file:handlers/__init__.py',
        kind: 'file',
        label: '__init__.py',
        file: 'handlers/__init__.py',
        line_start: 1,
        line_end: 16,
        area: 'handlers',
        parent: 'area:handlers',
      },
    ],
    edges: [],
  };

  it('returns just the node id for a leaf node', () => {
    const set = collectSubtreeNodeIds(
      compoundBlob,
      'dispatcher/router.py::dispatch',
    );
    expect(Array.from(set)).toEqual(['dispatcher/router.py::dispatch']);
  });

  it('walks the parent tree for compound (area / file) nodes', () => {
    const set = collectSubtreeNodeIds(compoundBlob, 'area:dispatcher');
    expect(set.has('area:dispatcher')).toBe(true);
    expect(set.has('file:dispatcher/router.py')).toBe(true);
    expect(set.has('dispatcher/router.py::dispatch')).toBe(true);
    // Nodes in a different area are not part of this subtree.
    expect(set.has('area:handlers')).toBe(false);
    expect(set.has('file:handlers/__init__.py')).toBe(false);
  });

  it('returns just the node id when the id is unknown', () => {
    const set = collectSubtreeNodeIds(compoundBlob, 'no-such-node');
    expect(Array.from(set)).toEqual(['no-such-node']);
  });
});

describe('groupEdgesByKind', () => {
  it('groups edges in canonical kind order', () => {
    const all = blob.edges as Edge[];
    const groups = groupEdgesByKind(all, () => true);
    // Canonical kind order in the component is calls → imports →
    // inherits → http; absent kinds are dropped.
    expect(groups.map((g) => g.kind)).toEqual(['calls', 'imports', 'inherits']);
  });

  it('skips edges that fail the predicate', () => {
    const groups = groupEdgesByKind(
      blob.edges as Edge[],
      (e) => e.target === 'agent/dog.py::Dog.bark',
    );
    expect(groups).toHaveLength(1);
    expect(groups[0].kind).toBe('calls');
    expect(groups[0].edges).toHaveLength(1);
  });
});

describe('NodeSidePanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the node label, source location and kind badge', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          file: 'agent/dog.py',
          line_start: 10,
          line_end: 20,
          content: 'def bark(self):\n    self.speak()\n',
        }),
      }),
    );
    wrap(
      <NodeSidePanel repoId={7} blob={blob} nodeId="agent/dog.py::Dog.bark" />,
    );
    expect(screen.getByTestId('node-side-panel-label').textContent).toBe(
      'Dog.bark',
    );
    expect(screen.getByTestId('node-side-panel-kind').textContent).toBe(
      'function',
    );
    expect(
      screen.getByTestId('node-side-panel-location').textContent,
    ).toContain('agent/dog.py:10-20');
  });

  it('lazily fetches the code preview from the endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        file: 'agent/dog.py',
        line_start: 10,
        line_end: 20,
        content: 'def bark(self):\n    self.speak()\n',
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    wrap(
      <NodeSidePanel repoId={7} blob={blob} nodeId="agent/dog.py::Dog.bark" />,
    );

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain('/api/repos/7/graph/code');
    expect(url).toContain('path=agent%2Fdog.py');
    expect(url).toContain('line_start=10');
    expect(url).toContain('line_end=20');

    await waitFor(() => {
      expect(screen.getByTestId('node-side-panel-code').textContent).toContain(
        'self.speak()',
      );
    });
  });

  it('lists incoming + outgoing edges grouped by kind for the selected node', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          file: 'agent/dog.py',
          line_start: 10,
          line_end: 20,
          content: '',
        }),
      }),
    );
    wrap(
      <NodeSidePanel repoId={7} blob={blob} nodeId="agent/dog.py::Dog.bark" />,
    );

    // Outgoing — 2 edges (calls + inherits).
    const outgoing = screen.getByTestId('node-side-panel-outgoing');
    expect(
      outgoing.querySelector(
        '[data-testid="node-side-panel-outgoing-group-calls"]',
      ),
    ).toBeTruthy();
    expect(
      outgoing.querySelector(
        '[data-testid="node-side-panel-outgoing-group-inherits"]',
      ),
    ).toBeTruthy();
    // Imports edge in the blob is between unrelated nodes — must not
    // surface here.
    expect(
      outgoing.querySelector(
        '[data-testid="node-side-panel-outgoing-group-imports"]',
      ),
    ).toBeNull();

    // Incoming — 1 calls edge from Cat.meow.
    const incoming = screen.getByTestId('node-side-panel-incoming');
    expect(
      incoming.querySelector(
        '[data-testid="node-side-panel-incoming-group-calls"]',
      ),
    ).toBeTruthy();
    expect(incoming.textContent).toContain('Cat.meow');
  });

  it('aggregates descendant edges for a compound (area) node', () => {
    // Regression test for the 2026-05-21 screenshot bug: selecting an
    // area node showed "Incoming edges: 0 / Outgoing edges: 0" even
    // though descendant function nodes had edges crossing into / out of
    // another area. The fix walks the parent tree so an area's
    // "outgoing" surfaces any edge whose source is somewhere inside it
    // and whose target is outside.
    const dispatcherBlob: RepoGraphBlob = {
      commit_sha: 'x',
      generated_at: '2026-05-21T00:00:00Z',
      analyser_version: 'phase7-multi-0.7.0',
      areas: [],
      nodes: [
        {
          id: 'area:dispatcher',
          kind: 'area',
          label: 'dispatcher',
          file: null,
          line_start: null,
          line_end: null,
          area: 'dispatcher',
          parent: null,
        },
        {
          id: 'file:dispatcher/router.py',
          kind: 'file',
          label: 'router.py',
          file: 'dispatcher/router.py',
          line_start: 1,
          line_end: 12,
          area: 'dispatcher',
          parent: 'area:dispatcher',
        },
        {
          id: 'dispatcher/router.py::dispatch',
          kind: 'function',
          label: 'dispatch',
          file: 'dispatcher/router.py',
          line_start: 6,
          line_end: 8,
          area: 'dispatcher',
          parent: 'file:dispatcher/router.py',
        },
        {
          id: 'dispatcher/router.py::dispatch_many',
          kind: 'function',
          label: 'dispatch_many',
          file: 'dispatcher/router.py',
          line_start: 10,
          line_end: 11,
          area: 'dispatcher',
          parent: 'file:dispatcher/router.py',
        },
        {
          id: 'area:handlers',
          kind: 'area',
          label: 'handlers',
          file: null,
          line_start: null,
          line_end: null,
          area: 'handlers',
          parent: null,
        },
        {
          id: 'file:handlers/__init__.py',
          kind: 'file',
          label: '__init__.py',
          file: 'handlers/__init__.py',
          line_start: 1,
          line_end: 16,
          area: 'handlers',
          parent: 'area:handlers',
        },
        {
          id: 'handlers/__init__.py::ping_handler',
          kind: 'function',
          label: 'ping_handler',
          file: 'handlers/__init__.py',
          line_start: 3,
          line_end: 4,
          area: 'handlers',
          parent: 'file:handlers/__init__.py',
        },
      ],
      edges: [
        // imports — crosses the dispatcher → handlers boundary.
        {
          source: 'file:dispatcher/router.py',
          target: 'file:handlers/__init__.py',
          kind: 'imports',
          evidence: {
            file: 'dispatcher/router.py',
            line: 1,
            snippet: 'from handlers import …',
          },
          source_kind: 'ast',
          boundary_violation: false,
        },
        // calls — also crosses the boundary.
        {
          source: 'dispatcher/router.py::dispatch',
          target: 'handlers/__init__.py::ping_handler',
          kind: 'calls',
          evidence: {
            file: 'dispatcher/router.py',
            line: 7,
            snippet: 'HANDLERS.get(name)',
          },
          source_kind: 'llm',
          boundary_violation: true,
          violation_reason: 'internal_access',
        },
        // internal — both endpoints inside dispatcher; must NOT show
        // up as either incoming or outgoing for area:dispatcher.
        {
          source: 'dispatcher/router.py::dispatch_many',
          target: 'dispatcher/router.py::dispatch',
          kind: 'calls',
          evidence: {
            file: 'dispatcher/router.py',
            line: 11,
            snippet: 'dispatch(name, p)',
          },
          source_kind: 'ast',
          boundary_violation: false,
        },
      ],
    };
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ file: null, line_start: null, line_end: null, content: '' }),
      }),
    );
    wrap(
      <NodeSidePanel
        repoId={7}
        blob={dispatcherBlob}
        nodeId="area:dispatcher"
      />,
    );

    // Outgoing — 1 imports edge + 1 calls edge crossing into handlers.
    expect(
      screen.getByTestId('node-side-panel-outgoing-count').textContent,
    ).toBe('2');
    // Incoming — nothing comes into the dispatcher subtree.
    expect(
      screen.getByTestId('node-side-panel-incoming-count').textContent,
    ).toBe('0');

    // The internal dispatch_many -> dispatch edge must not appear in
    // either direction — it lives inside the dispatcher subtree.
    const outgoing = screen.getByTestId('node-side-panel-outgoing');
    expect(outgoing.textContent).not.toContain('dispatch_many');
  });

  it('opens the edge-evidence popover when an edge row is clicked', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          file: 'agent/dog.py',
          line_start: 10,
          line_end: 20,
          content: '',
        }),
      }),
    );
    const onShow = vi.fn();
    wrap(
      <NodeSidePanel
        repoId={7}
        blob={blob}
        nodeId="agent/dog.py::Dog.bark"
        onShowEdgeEvidence={onShow}
      />,
    );
    const outgoing = screen.getByTestId('node-side-panel-outgoing');
    const row = outgoing.querySelector('[data-testid="edge-row"]');
    expect(row).toBeTruthy();
    fireEvent.click(row as Element);
    expect(onShow).toHaveBeenCalledTimes(1);
    const [edgeId, pos] = onShow.mock.calls[0];
    expect(edgeId).toMatch(/:calls$|:inherits$/);
    expect(pos).toEqual({ x: expect.any(Number), y: expect.any(Number) });
  });

  it('emits onSelectEdge when an edge row is clicked', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          file: 'agent/dog.py',
          line_start: 10,
          line_end: 20,
          content: '',
        }),
      }),
    );
    const handler = vi.fn();
    wrap(
      <NodeSidePanel
        repoId={7}
        blob={blob}
        nodeId="agent/dog.py::Dog.bark"
        onSelectEdge={handler}
      />,
    );
    const outgoing = screen.getByTestId('node-side-panel-outgoing');
    const rows = outgoing.querySelectorAll('[data-testid="edge-row"]');
    expect(rows.length).toBeGreaterThan(0);
    fireEvent.click(rows[0] as Element);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler.mock.calls[0][0]).toMatch(/:calls$|:inherits$/);
  });

  it('calls onClose when the X is clicked', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          file: 'agent/dog.py',
          line_start: 10,
          line_end: 20,
          content: '',
        }),
      }),
    );
    const onClose = vi.fn();
    wrap(
      <NodeSidePanel
        repoId={7}
        blob={blob}
        nodeId="agent/dog.py::Dog.bark"
        onClose={onClose}
      />,
    );
    fireEvent.click(screen.getByTestId('node-side-panel-close'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('returns null when the node id is not in the blob', () => {
    vi.stubGlobal('fetch', vi.fn());
    const { container } = wrap(
      <NodeSidePanel repoId={7} blob={blob} nodeId="does/not/exist" />,
    );
    expect(container.querySelector('[data-testid="node-side-panel"]')).toBeNull();
  });

  // ADR-016 Phase 7 P2 §11 — ancestor / descendant highlight controls.
  it('renders Highlight ancestors / descendants / clear controls', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          file: 'agent/dog.py',
          line_start: 10,
          line_end: 20,
          content: '',
        }),
      }),
    );
    wrap(
      <NodeSidePanel
        repoId={7}
        blob={blob}
        nodeId="agent/dog.py::Dog.bark"
        onHighlightReachability={() => {}}
      />,
    );
    expect(screen.getByTestId('reachability-ancestors')).toBeTruthy();
    expect(screen.getByTestId('reachability-descendants')).toBeTruthy();
    // Clear button only appears once a mode is active.
    expect(screen.queryByTestId('reachability-clear')).toBeNull();
  });

  it('emits ancestor set when Highlight ancestors is clicked', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          file: 'agent/dog.py',
          line_start: 10,
          line_end: 20,
          content: '',
        }),
      }),
    );
    const handler = vi.fn();
    wrap(
      <NodeSidePanel
        repoId={7}
        blob={blob}
        nodeId="agent/dog.py::Dog.bark"
        onHighlightReachability={handler}
      />,
    );
    fireEvent.click(screen.getByTestId('reachability-ancestors'));
    expect(handler).toHaveBeenCalledTimes(1);
    const arg = handler.mock.calls[0][0] as Set<string> | null;
    expect(arg).not.toBeNull();
    // Cat.meow calls Dog.bark, so it's an upstream caller.
    expect(arg!.has('agent/dog.py::Dog.bark')).toBe(true);
    expect(arg!.has('agent/cat.py::Cat.meow')).toBe(true);
  });

  it('emits descendant set when Highlight descendants is clicked', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          file: 'agent/dog.py',
          line_start: 10,
          line_end: 20,
          content: '',
        }),
      }),
    );
    const handler = vi.fn();
    wrap(
      <NodeSidePanel
        repoId={7}
        blob={blob}
        nodeId="agent/dog.py::Dog.bark"
        onHighlightReachability={handler}
      />,
    );
    fireEvent.click(screen.getByTestId('reachability-descendants'));
    const arg = handler.mock.calls[0][0] as Set<string> | null;
    expect(arg).not.toBeNull();
    // Dog.bark calls Dog.speak and inherits Animal — both are
    // downstream from Dog.bark.
    expect(arg!.has('agent/dog.py::Dog.bark')).toBe(true);
    expect(arg!.has('agent/dog.py::Dog.speak')).toBe(true);
    expect(arg!.has('agent/base.py::Animal')).toBe(true);
  });

  it('shows Clear once a mode is active and emits null on click', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          file: 'agent/dog.py',
          line_start: 10,
          line_end: 20,
          content: '',
        }),
      }),
    );
    const handler = vi.fn();
    wrap(
      <NodeSidePanel
        repoId={7}
        blob={blob}
        nodeId="agent/dog.py::Dog.bark"
        onHighlightReachability={handler}
      />,
    );
    fireEvent.click(screen.getByTestId('reachability-ancestors'));
    const clearBtn = screen.getByTestId('reachability-clear');
    expect(clearBtn).toBeTruthy();
    fireEvent.click(clearBtn);
    // Last call should be with null (clear).
    const last = handler.mock.calls[handler.mock.calls.length - 1][0];
    expect(last).toBeNull();
  });

  it('renders complexity badges for a function node with complexity fields', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          file: 'agent/dog.py',
          line_start: 10,
          line_end: 20,
          content: '',
        }),
      }),
    );
    const complexBlob: RepoGraphBlob = {
      commit_sha: 'abc',
      generated_at: '2026-05-15T00:00:00Z',
      analyser_version: 'phase7-0.7.0',
      areas: [],
      nodes: [
        {
          id: 'agent/dog.py::Dog.bark',
          kind: 'function',
          label: 'Dog.bark',
          file: 'agent/dog.py',
          line_start: 10,
          line_end: 20,
          area: 'agent',
          parent: 'class:agent/dog.py::Dog',
          cyclomatic: 7,
          cognitive: 4,
          loc: 22,
        },
      ],
      edges: [],
    };
    wrap(
      <NodeSidePanel
        repoId={7}
        blob={complexBlob}
        nodeId="agent/dog.py::Dog.bark"
      />,
    );
    const badges = screen.getByTestId('node-complexity');
    expect(badges).toBeTruthy();
    expect(badges.textContent).toContain('cyclomatic 7');
    expect(badges.textContent).toContain('cognitive 4');
    expect(badges.textContent).toContain('loc 22');
  });

  it('does not render complexity badges for a non-function node', () => {
    vi.stubGlobal('fetch', vi.fn());
    const fileBlob: RepoGraphBlob = {
      commit_sha: 'abc',
      generated_at: '2026-05-15T00:00:00Z',
      analyser_version: 'phase7-0.7.0',
      areas: [],
      nodes: [
        {
          id: 'file:agent/dog.py',
          kind: 'file',
          label: 'dog.py',
          file: 'agent/dog.py',
          line_start: null,
          line_end: null,
          area: 'agent',
          parent: 'area:agent',
        },
      ],
      edges: [],
    };
    wrap(
      <NodeSidePanel
        repoId={7}
        blob={fileBlob}
        nodeId="file:agent/dog.py"
      />,
    );
    expect(screen.queryByTestId('node-complexity')).toBeNull();
  });

  it('skips the code preview fetch for nodes without a file/line span', () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ file: '', line_start: 0, line_end: 0, content: '' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const areaBlob: RepoGraphBlob = {
      ...blob,
      nodes: [
        ...blob.nodes,
        {
          id: 'area:agent',
          kind: 'area',
          label: 'agent',
          file: null,
          line_start: null,
          line_end: null,
          area: 'agent',
          parent: null,
        },
      ],
    };
    wrap(<NodeSidePanel repoId={7} blob={areaBlob} nodeId="area:agent" />);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByTestId('node-side-panel-no-source')).toBeTruthy();
  });
});
