import { describe, it, expect } from 'vitest';
import {
  blobToCytoscapeElements,
  computeSearchClasses,
} from '@/components/code-graph/graph-canvas';
import type { RepoGraphBlob } from '@/types/api';

const blob: RepoGraphBlob = {
  commit_sha: 'abc',
  generated_at: '2026-05-15T00:00:00Z',
  analyser_version: 'phase2-python-0.2.0',
  areas: [
    { name: 'agent', status: 'ok', error: null, unresolved_dynamic_sites: 0 },
    {
      name: 'orchestrator',
      status: 'failed',
      error: 'parser blew up',
      unresolved_dynamic_sites: 0,
    },
  ],
  nodes: [
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
    {
      id: 'area:orchestrator',
      kind: 'area',
      label: 'orchestrator',
      file: null,
      line_start: null,
      line_end: null,
      area: 'orchestrator',
      parent: null,
    },
    {
      id: 'file:agent/dog.py',
      kind: 'file',
      label: 'dog.py',
      file: 'agent/dog.py',
      line_start: 1,
      line_end: 10,
      area: 'agent',
      parent: 'area:agent',
    },
  ],
  edges: [
    {
      source: 'file:agent/dog.py',
      target: 'module:agent_area.base',
      kind: 'imports',
      evidence: { file: 'agent/dog.py', line: 1, snippet: 'from agent_area.base import Animal' },
      source_kind: 'ast',
      boundary_violation: false,
    },
    {
      source: 'agent/dog.py::Dog.describe',
      target: 'agent/dog.py::Dog.speak',
      kind: 'calls',
      evidence: { file: 'agent/dog.py', line: 5, snippet: 'self.speak()' },
      source_kind: 'ast',
      boundary_violation: false,
    },
  ],
};

describe('blobToCytoscapeElements', () => {
  it('marks failed areas with failed=1', () => {
    const els = blobToCytoscapeElements(blob, {
      'area:agent': null,
      'area:orchestrator': 'parser blew up',
    });
    const agent = els.find((e) => e.data.id === 'area:agent');
    const orch = els.find((e) => e.data.id === 'area:orchestrator');
    expect(agent!.data.failed).toBeUndefined();
    expect(orch!.data.failed).toBe(1);
    expect(orch!.data.error).toBe('parser blew up');
  });

  it('preserves parent hierarchy for non-area nodes', () => {
    const els = blobToCytoscapeElements(blob, {});
    const file = els.find((e) => e.data.id === 'file:agent/dog.py');
    expect(file!.data.parent).toBe('area:agent');
  });

  it('colour-codes edges by kind', () => {
    const els = blobToCytoscapeElements(blob, {});
    const imports = els.find((e) => e.data.kind === 'imports');
    const calls = els.find((e) => e.data.kind === 'calls');
    expect(imports!.data.color).toBe('#9ca3af');
    expect(calls!.data.color).toBe('#3b82f6');
    // Carries through ``source_kind`` + evidence for future Phase 7 features.
    expect(imports!.data.sourceKind).toBe('ast');
    expect(imports!.data.snippet).toMatch(/Animal/);
  });

  it('surfaces boundary_violation and violation_reason on edge data', () => {
    // Phase 5 — flagged edges carry ``boundaryViolation=1`` and the
    // reason string so the cytoscape selector can apply a destructive
    // overlay and the side panel can correlate rows.
    const flaggedBlob: RepoGraphBlob = {
      ...blob,
      edges: [
        {
          source: 'agent/dog.py::Dog.describe',
          target: 'agent/dog.py::Dog.speak',
          kind: 'calls',
          evidence: {
            file: 'agent/dog.py',
            line: 5,
            snippet: 'self.speak()',
          },
          source_kind: 'ast',
          boundary_violation: true,
          violation_reason: 'internal_access',
        },
      ],
    };
    const els = blobToCytoscapeElements(flaggedBlob, {});
    const edge = els.find((e) => e.data.kind === 'calls');
    expect(edge!.data.boundaryViolation).toBe(1);
    expect(edge!.data.violationReason).toBe('internal_access');
    // Colour switches to the destructive overlay so the cytoscape style
    // selector has data to bind onto.
    expect(edge!.data.color).toBe('#ef4444');
  });

  it('marks the highlighted edge id with highlighted=1', () => {
    const els = blobToCytoscapeElements(blob, {}, {
      highlightedEdgeId: 'agent/dog.py::Dog.describe->agent/dog.py::Dog.speak:calls',
    });
    const calls = els.find((e) => e.data.kind === 'calls');
    expect(calls!.data.highlighted).toBe(1);
    const imports = els.find((e) => e.data.kind === 'imports');
    expect(imports!.data.highlighted).toBeUndefined();
  });
});

// ADR-016 Phase 7 §11 — search controls.
describe('computeSearchClasses', () => {
  it('returns empty matches and empty fades for an empty query', () => {
    const result = computeSearchClasses(blob, '');
    expect(result.matches.size).toBe(0);
    expect(result.fades.size).toBe(0);
  });

  it('treats a whitespace-only query as empty', () => {
    const result = computeSearchClasses(blob, '   ');
    expect(result.matches.size).toBe(0);
    expect(result.fades.size).toBe(0);
  });

  it('case-insensitively matches substring on node.label', () => {
    // Two nodes have label "agent"/"orchestrator"; only "dog.py"
    // matches "DOG".
    const result = computeSearchClasses(blob, 'DOG');
    expect(result.matches.has('file:agent/dog.py')).toBe(true);
    expect(result.matches.has('area:agent')).toBe(false);
    // Non-matching nodes are faded.
    expect(result.fades.has('area:agent')).toBe(true);
    expect(result.fades.has('area:orchestrator')).toBe(true);
  });

  it('matches are not in fades and vice versa', () => {
    const result = computeSearchClasses(blob, 'agent');
    // Both "area:agent" (label "agent") and "file:agent/dog.py" (label
    // "dog.py") — only the first matches by label.
    for (const id of result.matches) {
      expect(result.fades.has(id)).toBe(false);
    }
  });
});
