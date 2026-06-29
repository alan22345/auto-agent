import { describe, it, expect } from 'vitest';
import { blobToCytoscapeElements } from '@/components/code-graph/graph-elements';
import { computeSearchClasses } from '@/components/code-graph/graph-canvas';
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
    {
      id: 'file:agent/base.py',
      kind: 'file',
      label: 'base.py',
      file: 'agent/base.py',
      line_start: 1,
      line_end: 10,
      area: 'agent',
      parent: 'area:agent',
    },
    {
      id: 'agent/dog.py::Dog.describe',
      kind: 'function',
      label: 'describe',
      file: 'agent/dog.py',
      line_start: 3,
      line_end: 5,
      area: 'agent',
      parent: 'file:agent/dog.py',
    },
    {
      id: 'agent/dog.py::Dog.speak',
      kind: 'function',
      label: 'speak',
      file: 'agent/dog.py',
      line_start: 6,
      line_end: 7,
      area: 'agent',
      parent: 'file:agent/dog.py',
    },
  ],
  edges: [
    {
      source: 'file:agent/dog.py',
      target: 'file:agent/base.py',
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

  // ADR-016 Phase 7 §11 — AST vs LLM visual distinction. The data flag
  // is what the cytoscape selector ``edge[?sourceKindLlm]`` keys off to
  // paint LLM-deduced edges with a dotted line.
  it('sets sourceKindLlm=true on edges with source_kind="llm"', () => {
    const llmBlob: RepoGraphBlob = {
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
          source_kind: 'llm',
          boundary_violation: false,
        },
      ],
    };
    const els = blobToCytoscapeElements(llmBlob, {});
    const edge = els.find((e) => e.data.kind === 'calls');
    expect(edge!.data.sourceKindLlm).toBe(true);
    // Source kind itself is still carried for the evidence popover.
    expect(edge!.data.sourceKind).toBe('llm');
  });

  it('drops edges whose endpoints are not in the node set', () => {
    // Regression test for the 2026-05-21 "canvas renders blank" bug:
    // cytoscape silently rejects edges referencing non-existent nodes
    // AND its compound layout breaks when phantom edges are present,
    // leaving every node positioned at the origin. The builder must
    // filter those out before handing elements to cytoscape.
    const orphanBlob: RepoGraphBlob = {
      ...blob,
      edges: [
        // Phantom source — ``module:dispatcher.router`` is not a node.
        {
          source: 'module:dispatcher.router',
          target: 'file:agent/dog.py',
          kind: 'imports',
          evidence: { file: 'dispatcher/router.py', line: 1, snippet: 'from agent import dog' },
          source_kind: 'ast',
          boundary_violation: false,
        },
        // Phantom target — ``module:handlers`` is not a node.
        {
          source: 'file:agent/dog.py',
          target: 'module:handlers',
          kind: 'imports',
          evidence: { file: 'agent/dog.py', line: 2, snippet: 'from handlers import HANDLERS' },
          source_kind: 'ast',
          boundary_violation: false,
        },
        // Valid edge — both endpoints exist.
        {
          source: 'file:agent/dog.py',
          target: 'file:agent/base.py',
          kind: 'imports',
          evidence: { file: 'agent/dog.py', line: 1, snippet: 'from agent.base import Animal' },
          source_kind: 'ast',
          boundary_violation: false,
        },
      ],
    };
    const els = blobToCytoscapeElements(orphanBlob, {});
    const edges = els.filter((e) => e.data.source !== undefined);
    expect(edges).toHaveLength(1);
    expect(edges[0]!.data.source).toBe('file:agent/dog.py');
    expect(edges[0]!.data.target).toBe('file:agent/base.py');
  });

  it('omits sourceKindLlm on edges with source_kind="ast"', () => {
    // Keep the property absent rather than ``false`` — the cytoscape
    // ``[?sourceKindLlm]`` selector treats both undefined and false as
    // "no match", but absence is the convention used elsewhere in this
    // builder (``highlighted``, ``boundaryViolation``) so leaving the
    // key off keeps the element data tight.
    const els = blobToCytoscapeElements(blob, {});
    for (const e of els) {
      if (e.data.source === undefined) continue; // skip nodes
      expect(e.data.sourceKindLlm).toBeUndefined();
    }
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
