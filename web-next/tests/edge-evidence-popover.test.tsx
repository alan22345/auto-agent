import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { EdgeEvidencePopover } from '@/components/code-graph/edge-evidence-popover';
import type { RepoGraphBlob } from '@/types/api';

const baseBlob: RepoGraphBlob = {
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
      parent: null,
    },
    {
      id: 'agent/dog.py::Dog.speak',
      kind: 'function',
      label: 'Dog.speak',
      file: 'agent/dog.py',
      line_start: 22,
      line_end: 30,
      area: 'agent',
      parent: null,
    },
  ],
  edges: [
    {
      source: 'agent/dog.py::Dog.bark',
      target: 'agent/dog.py::Dog.speak',
      kind: 'calls',
      evidence: { file: 'agent/dog.py', line: 11, snippet: 'self.speak()' },
      source_kind: 'ast',
      boundary_violation: false,
    },
  ],
};

const edgeId = 'agent/dog.py::Dog.bark->agent/dog.py::Dog.speak:calls';

function llmBlob(): RepoGraphBlob {
  return {
    ...baseBlob,
    edges: [{ ...baseBlob.edges[0], source_kind: 'llm' }],
  };
}

function violationBlob(): RepoGraphBlob {
  return {
    ...baseBlob,
    edges: [
      {
        ...baseBlob.edges[0],
        boundary_violation: true,
        violation_reason: 'internal_access',
      },
    ],
  };
}

describe('EdgeEvidencePopover', () => {
  it('renders kind chip, endpoints, evidence and source_kind tag', () => {
    render(
      <EdgeEvidencePopover
        blob={baseBlob}
        edgeId={edgeId}
        position={{ x: 100, y: 100 }}
        onClose={() => {}}
      />,
    );
    expect(screen.getByTestId('edge-evidence-kind').textContent).toBe('calls');
    expect(screen.getByTestId('edge-evidence-source-kind').textContent).toBe(
      'ast',
    );
    expect(screen.getByTestId('edge-evidence-endpoints').textContent).toMatch(
      /Dog\.bark.*Dog\.speak/,
    );
    expect(screen.getByTestId('edge-evidence-location').textContent).toBe(
      'agent/dog.py:11',
    );
    expect(screen.getByTestId('edge-evidence-snippet').textContent).toContain(
      'self.speak()',
    );
  });

  it('surfaces source_kind=llm in the tag', () => {
    render(
      <EdgeEvidencePopover
        blob={llmBlob()}
        edgeId={edgeId}
        position={{ x: 0, y: 0 }}
        onClose={() => {}}
      />,
    );
    expect(screen.getByTestId('edge-evidence-source-kind').textContent).toBe(
      'llm',
    );
  });

  it('renders the violation reason in red when boundary_violation=true', () => {
    render(
      <EdgeEvidencePopover
        blob={violationBlob()}
        edgeId={edgeId}
        position={{ x: 0, y: 0 }}
        onClose={() => {}}
      />,
    );
    const violation = screen.getByTestId('edge-evidence-violation');
    expect(violation.textContent).toContain('internal_access');
    // The destructive style is asserted via the test-friendly role.
    expect(violation.getAttribute('role')).toBe('alert');
  });

  it('closes on click-outside', () => {
    const onClose = vi.fn();
    render(
      <div>
        <button type="button" data-testid="outside">
          outside
        </button>
        <EdgeEvidencePopover
          blob={baseBlob}
          edgeId={edgeId}
          position={{ x: 0, y: 0 }}
          onClose={onClose}
        />
      </div>,
    );
    fireEvent.mouseDown(screen.getByTestId('outside'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does NOT close when the user clicks inside the popover', () => {
    const onClose = vi.fn();
    render(
      <EdgeEvidencePopover
        blob={baseBlob}
        edgeId={edgeId}
        position={{ x: 0, y: 0 }}
        onClose={onClose}
      />,
    );
    fireEvent.mouseDown(screen.getByTestId('edge-evidence-snippet'));
    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes on Escape', () => {
    const onClose = vi.fn();
    render(
      <EdgeEvidencePopover
        blob={baseBlob}
        edgeId={edgeId}
        position={{ x: 0, y: 0 }}
        onClose={onClose}
      />,
    );
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('renders nothing if the edge id is unknown', () => {
    const { container } = render(
      <EdgeEvidencePopover
        blob={baseBlob}
        edgeId="ghost->edge:calls"
        position={{ x: 0, y: 0 }}
        onClose={() => {}}
      />,
    );
    expect(
      container.querySelector('[data-testid="edge-evidence-popover"]'),
    ).toBeNull();
    // The portal renders into document.body, not container — so also
    // assert globally.
    expect(
      document.body.querySelector('[data-testid="edge-evidence-popover"]'),
    ).toBeNull();
  });

  it('emits onClose when the X is clicked', () => {
    const onClose = vi.fn();
    render(
      <EdgeEvidencePopover
        blob={baseBlob}
        edgeId={edgeId}
        position={{ x: 0, y: 0 }}
        onClose={onClose}
      />,
    );
    fireEvent.click(screen.getByTestId('edge-evidence-close'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
