import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import {
  findViolations,
  ViolationsPanel,
  violationEdgeId,
} from '@/components/code-graph/violations-panel';
import type { RepoGraphBlob } from '@/types/api';

const _NODES = [
  {
    id: 'area_a/caller.py::use_private',
    kind: 'function' as const,
    label: 'use_private',
    file: 'area_a/caller.py',
    line_start: 1,
    line_end: 5,
    area: 'area_a',
    parent: 'file:area_a/caller.py',
  },
  {
    id: 'area_b/public_api.py::_private_helper',
    kind: 'function' as const,
    label: '_private_helper',
    file: 'area_b/public_api.py',
    line_start: 14,
    line_end: 15,
    area: 'area_b',
    parent: 'file:area_b/public_api.py',
  },
];

function blobWithViolation(): RepoGraphBlob {
  return {
    commit_sha: 'abc',
    generated_at: '2026-05-15T00:00:00Z',
    analyser_version: 'phase5-multi-0.5.0',
    areas: [],
    nodes: _NODES,
    edges: [
      {
        source: 'area_a/caller.py::use_private',
        target: 'area_b/public_api.py::_private_helper',
        kind: 'calls',
        evidence: {
          file: 'area_a/caller.py',
          line: 13,
          snippet: 'return _private_helper()',
        },
        source_kind: 'ast',
        boundary_violation: true,
        violation_reason: 'internal_access',
      },
    ],
  };
}

function emptyBlob(): RepoGraphBlob {
  return {
    commit_sha: 'abc',
    generated_at: '2026-05-15T00:00:00Z',
    analyser_version: 'phase5-multi-0.5.0',
    areas: [],
    nodes: [],
    edges: [],
  };
}

describe('findViolations', () => {
  it('skips edges without boundary_violation set to true', () => {
    const blob: RepoGraphBlob = {
      ...emptyBlob(),
      edges: [
        {
          source: 'a',
          target: 'b',
          kind: 'calls',
          evidence: { file: 'a.py', line: 1, snippet: 'x' },
          source_kind: 'ast',
          boundary_violation: false,
        },
      ],
    };
    expect(findViolations(blob)).toEqual([]);
  });

  it('resolves source / target labels from nodes when available', () => {
    const rows = findViolations(blobWithViolation());
    expect(rows).toHaveLength(1);
    expect(rows[0]?.sourceLabel).toBe('use_private');
    expect(rows[0]?.targetLabel).toBe('_private_helper');
    expect(rows[0]?.reason).toBe('internal_access');
  });
});

describe('ViolationsPanel', () => {
  it('renders the violation count in the collapsed header badge', () => {
    render(<ViolationsPanel blob={blobWithViolation()} />);
    expect(screen.getByTestId('violations-count').textContent).toBe('1');
    // Body is hidden by default.
    expect(screen.queryByTestId('violations-panel-body')).toBeNull();
  });

  it('expands and lists rows after clicking the header', () => {
    render(<ViolationsPanel blob={blobWithViolation()} />);
    fireEvent.click(screen.getByRole('button', { name: /Boundary violations/i }));
    const body = screen.getByTestId('violations-panel-body');
    expect(body).toBeTruthy();
    expect(body.textContent).toContain('use_private');
    expect(body.textContent).toContain('_private_helper');
    expect(body.textContent).toContain('internal_access');
  });

  it('renders the trust-building empty state when there are no violations', () => {
    render(<ViolationsPanel blob={emptyBlob()} />);
    fireEvent.click(screen.getByRole('button', { name: /Boundary violations/i }));
    const body = screen.getByTestId('violations-panel-body');
    expect(body.textContent).toMatch(
      /No boundary violations detected.*Public-surface inferred.*explicit rules from .auto-agent\/graph\.yml/s,
    );
  });

  it('invokes onSelectEdge with the row edge id when a row is clicked', () => {
    const handler = vi.fn();
    render(
      <ViolationsPanel
        blob={blobWithViolation()}
        onSelectEdge={handler}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /Boundary violations/i }));
    fireEvent.click(screen.getByTestId('violation-row'));
    expect(handler).toHaveBeenCalledWith(
      violationEdgeId(blobWithViolation().edges[0]),
    );
  });

  it('toggles selection off when the highlighted row is clicked again', () => {
    const blob = blobWithViolation();
    const edgeId = violationEdgeId(blob.edges[0]);
    const handler = vi.fn();
    render(
      <ViolationsPanel
        blob={blob}
        highlightedEdgeId={edgeId}
        onSelectEdge={handler}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /Boundary violations/i }));
    fireEvent.click(screen.getByTestId('violation-row'));
    expect(handler).toHaveBeenCalledWith(null);
  });
});
