import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { NodeSidePanel, groupEdgesByKind } from '@/components/code-graph/node-side-panel';
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
