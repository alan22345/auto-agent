# Code-graph Health Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the code-graph quality dimensions (repo health score, cycles, dead code, clones, hotspots, file health) and per-node complexity on the `code-graph/[repoId]` page via a new "Health" tab.

**Architecture:** No backend changes. `GET /api/repos/{id}/graph/latest` already returns the full `RepoGraphBlob` including all quality fields; they are just untyped and unrendered. We regenerate the TS types, build one small focused component per dimension under `web-next/components/code-graph/`, compose them in a `HealthTab` container, add a third tab to the page, and add complexity badges to the existing node side-panel.

**Tech Stack:** Next.js (App Router) + TypeScript, Tailwind, shadcn/ui (`card`, `badge`, `button`), vitest + `@testing-library/react`. Tests live in `web-next/tests/`. All commands run from `web-next/`.

---

## File Structure

**Changed:**
- `web-next/types/api.ts` — regenerated (adds `RepoHealth`, `DependencyCycle`, `DeadCodeFinding`, `CloneGroup`, `CloneInstance`, `Hotspot`, `FileHealth`; adds quality fields to `RepoGraphBlob` and complexity fields to `Node`).
- `web-next/app/(app)/code-graph/[repoId]/page.tsx` — add the `health` tab.
- `web-next/components/code-graph/node-side-panel.tsx` — complexity badges.

**New (all under `web-next/components/code-graph/`):**
- `collapsible-section.tsx` — shared collapsible wrapper (header + count badge + chevron).
- `health-scorecard.tsx` — score bar + count cards.
- `cycles-section.tsx`, `dead-code-section.tsx`, `clones-section.tsx`, `hotspots-section.tsx`, `file-health-section.tsx` — one per dimension.
- `health-tab.tsx` — container composing the scorecard + six sections, with stale/empty handling.

**New tests (under `web-next/tests/`):** `collapsible-section.test.tsx`, `health-scorecard.test.tsx`, `cycles-section.test.tsx`, `dead-code-section.test.tsx`, `clones-section.test.tsx`, `hotspots-section.test.tsx`, `file-health-section.test.tsx`, `health-tab.test.tsx`. Plus additions to existing `tests/node-side-panel.test.tsx` and `tests/code-graph-page.test.tsx`.

---

### Task 1: Regenerate TypeScript types

**Files:**
- Modify: `web-next/types/api.ts` (generated)

- [ ] **Step 1: Run the generator**

Run (from repo root): `python3.12 scripts/gen_ts_types.py`
Expected: script completes; `web-next/types/api.ts` is rewritten.

- [ ] **Step 2: Verify the quality types now exist**

Run (from repo root): `grep -E "interface (RepoHealth|DependencyCycle|DeadCodeFinding|CloneGroup|CloneInstance|Hotspot|FileHealth)" web-next/types/api.ts`
Expected: all seven interfaces print. Also verify fields landed:
Run: `grep -E "cyclomatic|cognitive|file_health|hotspots|dead_code" web-next/types/api.ts`
Expected: matches in `Node` and `RepoGraphBlob`.

- [ ] **Step 3: Typecheck still passes**

Run (from `web-next/`): `npm run typecheck`
Expected: no errors (no consumer references the new fields yet).

- [ ] **Step 4: Commit**

```bash
git add web-next/types/api.ts
git commit -m "chore(web-next): regen api types with code-graph quality fields"
```

---

### Task 2: CollapsibleSection (shared wrapper)

**Files:**
- Create: `web-next/components/code-graph/collapsible-section.tsx`
- Test: `web-next/tests/collapsible-section.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// web-next/tests/collapsible-section.test.tsx
import { describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { CollapsibleSection } from '@/components/code-graph/collapsible-section';

describe('CollapsibleSection', () => {
  it('shows the count and hides children until expanded', () => {
    render(
      <CollapsibleSection title="Cycles" count={3} testId="sec">
        <p>body content</p>
      </CollapsibleSection>,
    );
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.queryByText('body content')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText('body content')).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- collapsible-section`
Expected: FAIL — cannot resolve `@/components/code-graph/collapsible-section`.

- [ ] **Step 3: Write minimal implementation**

```tsx
// web-next/components/code-graph/collapsible-section.tsx
'use client';
import { useState, type ReactNode } from 'react';
import { Badge } from '@/components/ui/badge';
import { ChevronRight } from 'lucide-react';

interface Props {
  title: string;
  count: number;
  testId?: string;
  defaultOpen?: boolean;
  children: ReactNode;
}

export function CollapsibleSection({
  title,
  count,
  testId,
  defaultOpen = false,
  children,
}: Props) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section data-testid={testId} className="rounded-md border bg-card/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left"
      >
        <span className="flex items-center gap-2 text-sm font-semibold">
          <ChevronRight
            size={14}
            className={`transition-transform ${open ? 'rotate-90' : ''}`}
          />
          {title}
        </span>
        <Badge variant={count > 0 ? 'secondary' : 'outline'}>{count}</Badge>
      </button>
      {open && <div className="border-t px-3 py-2 text-sm">{children}</div>}
    </section>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test -- collapsible-section`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web-next/components/code-graph/collapsible-section.tsx web-next/tests/collapsible-section.test.tsx
git commit -m "feat(code-graph): add CollapsibleSection wrapper"
```

---

### Task 3: HealthScorecard

**Files:**
- Create: `web-next/components/code-graph/health-scorecard.tsx`
- Test: `web-next/tests/health-scorecard.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// web-next/tests/health-scorecard.test.tsx
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { HealthScorecard } from '@/components/code-graph/health-scorecard';
import type { RepoHealth } from '@/types/api';

const health: RepoHealth = {
  score: 72.4,
  clone_count: 5,
  cycle_count: 3,
  dead_count: 8,
  hotspot_count: 12,
};

describe('HealthScorecard', () => {
  it('renders the rounded score and all counts', () => {
    render(<HealthScorecard health={health} poorFileCount={4} />);
    expect(screen.getByTestId('health-score')).toHaveTextContent('72');
    expect(screen.getByTestId('count-Cycles')).toHaveTextContent('3');
    expect(screen.getByTestId('count-Clones')).toHaveTextContent('5');
    expect(screen.getByTestId('count-Dead code')).toHaveTextContent('8');
    expect(screen.getByTestId('count-Hotspots')).toHaveTextContent('12');
    expect(screen.getByTestId('count-Poor files')).toHaveTextContent('4');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- health-scorecard`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```tsx
// web-next/components/code-graph/health-scorecard.tsx
'use client';
import { Card } from '@/components/ui/card';
import type { RepoHealth } from '@/types/api';

interface Props {
  health: RepoHealth;
  poorFileCount: number;
}

export function HealthScorecard({ health, poorFileCount }: Props) {
  const score = Math.round(health.score);
  const pct = Math.max(0, Math.min(100, score));
  return (
    <div data-testid="health-scorecard" className="space-y-3">
      <div>
        <div className="flex items-baseline justify-between">
          <span className="text-sm font-semibold">Repo health</span>
          <span
            data-testid="health-score"
            className="text-2xl font-bold tabular-nums"
          >
            {score}
            <span className="text-sm font-normal text-muted-foreground">
              /100
            </span>
          </span>
        </div>
        <div className="mt-1 h-2 w-full overflow-hidden rounded-full bg-muted">
          <div
            className="h-full rounded-full bg-primary"
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        <CountCard label="Cycles" value={health.cycle_count} />
        <CountCard label="Clones" value={health.clone_count} />
        <CountCard label="Dead code" value={health.dead_count} />
        <CountCard label="Hotspots" value={health.hotspot_count} />
        <CountCard label="Poor files" value={poorFileCount} />
      </div>
    </div>
  );
}

function CountCard({ label, value }: { label: string; value: number }) {
  return (
    <Card className="p-3 text-center">
      <p
        data-testid={`count-${label}`}
        className="text-xl font-bold tabular-nums"
      >
        {value}
      </p>
      <p className="text-xs text-muted-foreground">{label}</p>
    </Card>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test -- health-scorecard`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web-next/components/code-graph/health-scorecard.tsx web-next/tests/health-scorecard.test.tsx
git commit -m "feat(code-graph): add HealthScorecard"
```

---

### Task 4: CyclesSection

**Files:**
- Create: `web-next/components/code-graph/cycles-section.tsx`
- Test: `web-next/tests/cycles-section.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// web-next/tests/cycles-section.test.tsx
import { describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { CyclesSection } from '@/components/code-graph/cycles-section';
import type { DependencyCycle } from '@/types/api';

const cycles: DependencyCycle[] = [
  { id: 'c1', kind: 'import', members: ['agent/x', 'agent/y'], closing_edges: [] },
];

describe('CyclesSection', () => {
  it('renders the member chain when expanded', () => {
    render(<CyclesSection cycles={cycles} />);
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByTestId('cycle-row')).toHaveTextContent('agent/x → agent/y');
  });

  it('renders an empty state with zero cycles', () => {
    render(<CyclesSection cycles={[]} />);
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText(/no dependency cycles/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- cycles-section`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```tsx
// web-next/components/code-graph/cycles-section.tsx
'use client';
import { CollapsibleSection } from './collapsible-section';
import type { DependencyCycle } from '@/types/api';

export function CyclesSection({ cycles }: { cycles: DependencyCycle[] }) {
  return (
    <CollapsibleSection title="Cycles" count={cycles.length} testId="cycles-section">
      {cycles.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No dependency cycles detected.
        </p>
      ) : (
        <ul className="space-y-2">
          {cycles.map((c) => (
            <li key={c.id} data-testid="cycle-row" className="text-xs">
              <span className="mr-2 rounded bg-muted px-1.5 py-0.5 font-semibold uppercase">
                {c.kind}
              </span>
              <span className="break-all font-mono">
                {c.members.join(' → ')} → {c.members[0]}
              </span>
            </li>
          ))}
        </ul>
      )}
    </CollapsibleSection>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test -- cycles-section`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web-next/components/code-graph/cycles-section.tsx web-next/tests/cycles-section.test.tsx
git commit -m "feat(code-graph): add CyclesSection"
```

---

### Task 5: DeadCodeSection

**Files:**
- Create: `web-next/components/code-graph/dead-code-section.tsx`
- Test: `web-next/tests/dead-code-section.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// web-next/tests/dead-code-section.test.tsx
import { describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { DeadCodeSection } from '@/components/code-graph/dead-code-section';
import type { DeadCodeFinding } from '@/types/api';

const dead: DeadCodeFinding[] = [
  { kind: 'unused_export', target: 'agent/x.py::foo', file: 'agent/x.py', reason: 'no importers' },
];

describe('DeadCodeSection', () => {
  it('renders a row per finding when expanded', () => {
    render(<DeadCodeSection deadCode={dead} />);
    fireEvent.click(screen.getByRole('button'));
    const row = screen.getByTestId('dead-code-row');
    expect(row).toHaveTextContent('unused_export');
    expect(row).toHaveTextContent('agent/x.py::foo');
    expect(row).toHaveTextContent('no importers');
  });

  it('renders an empty state', () => {
    render(<DeadCodeSection deadCode={[]} />);
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText(/no dead code/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- dead-code-section`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```tsx
// web-next/components/code-graph/dead-code-section.tsx
'use client';
import { CollapsibleSection } from './collapsible-section';
import type { DeadCodeFinding } from '@/types/api';

export function DeadCodeSection({
  deadCode,
}: {
  deadCode: DeadCodeFinding[];
}) {
  return (
    <CollapsibleSection
      title="Dead code"
      count={deadCode.length}
      testId="dead-code-section"
    >
      {deadCode.length === 0 ? (
        <p className="text-xs text-muted-foreground">No dead code detected.</p>
      ) : (
        <table className="w-full text-left text-xs">
          <thead className="text-muted-foreground">
            <tr>
              <th className="py-1 pr-2 font-medium">Kind</th>
              <th className="py-1 pr-2 font-medium">Target</th>
              <th className="py-1 pr-2 font-medium">File</th>
              <th className="py-1 font-medium">Reason</th>
            </tr>
          </thead>
          <tbody>
            {deadCode.map((d, i) => (
              <tr
                key={`${d.kind}:${d.target}:${i}`}
                data-testid="dead-code-row"
                className="border-t"
              >
                <td className="py-1 pr-2 font-mono">{d.kind}</td>
                <td className="break-all py-1 pr-2 font-mono">{d.target}</td>
                <td className="break-all py-1 pr-2 font-mono">{d.file ?? '—'}</td>
                <td className="py-1">{d.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </CollapsibleSection>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test -- dead-code-section`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web-next/components/code-graph/dead-code-section.tsx web-next/tests/dead-code-section.test.tsx
git commit -m "feat(code-graph): add DeadCodeSection"
```

---

### Task 6: ClonesSection

**Files:**
- Create: `web-next/components/code-graph/clones-section.tsx`
- Test: `web-next/tests/clones-section.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// web-next/tests/clones-section.test.tsx
import { describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ClonesSection } from '@/components/code-graph/clones-section';
import type { CloneGroup } from '@/types/api';

const clones: CloneGroup[] = [
  {
    id: 'g1',
    token_len: 120,
    mode: 'strict',
    family_id: null,
    instances: [
      { node_id: 'a', file: 'agent/a.py', line_start: 10, line_end: 30 },
      { node_id: 'b', file: 'agent/b.py', line_start: 5, line_end: 25 },
    ],
  },
];

describe('ClonesSection', () => {
  it('renders the family with its instance locations', () => {
    render(<ClonesSection clones={clones} />);
    fireEvent.click(screen.getByRole('button'));
    const row = screen.getByTestId('clone-row');
    expect(row).toHaveTextContent('strict');
    expect(row).toHaveTextContent('agent/a.py:10-30');
    expect(row).toHaveTextContent('agent/b.py:5-25');
  });

  it('renders an empty state', () => {
    render(<ClonesSection clones={[]} />);
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText(/no code clones/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- clones-section`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```tsx
// web-next/components/code-graph/clones-section.tsx
'use client';
import { CollapsibleSection } from './collapsible-section';
import type { CloneGroup } from '@/types/api';

export function ClonesSection({ clones }: { clones: CloneGroup[] }) {
  return (
    <CollapsibleSection title="Clones" count={clones.length} testId="clones-section">
      {clones.length === 0 ? (
        <p className="text-xs text-muted-foreground">No code clones detected.</p>
      ) : (
        <ul className="space-y-2">
          {clones.map((g) => (
            <li key={g.id} data-testid="clone-row" className="text-xs">
              <p className="font-semibold">
                <span className="mr-2 rounded bg-muted px-1.5 py-0.5 uppercase">
                  {g.mode}
                </span>
                {g.instances.length} instances · {g.token_len} tokens
              </p>
              <ul className="ml-3 mt-1 space-y-0.5 font-mono text-muted-foreground">
                {g.instances.map((inst) => (
                  <li key={inst.node_id} className="break-all">
                    {inst.file}:{inst.line_start}-{inst.line_end}
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </CollapsibleSection>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test -- clones-section`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web-next/components/code-graph/clones-section.tsx web-next/tests/clones-section.test.tsx
git commit -m "feat(code-graph): add ClonesSection"
```

---

### Task 7: HotspotsSection

**Files:**
- Create: `web-next/components/code-graph/hotspots-section.tsx`
- Test: `web-next/tests/hotspots-section.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// web-next/tests/hotspots-section.test.tsx
import { describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { HotspotsSection } from '@/components/code-graph/hotspots-section';
import type { Hotspot } from '@/types/api';

const hotspots: Hotspot[] = [
  { file: 'agent/low.py', churn: 0.2, complexity_density: 0.3, score: 0.40, trend: 'cooling' },
  { file: 'orchestrator/router.py', churn: 0.81, complexity_density: 0.9, score: 0.91, trend: 'accelerating' },
];

describe('HotspotsSection', () => {
  it('renders rows sorted by score descending', () => {
    render(<HotspotsSection hotspots={hotspots} />);
    fireEvent.click(screen.getByRole('button'));
    const rows = screen.getAllByTestId('hotspot-row');
    expect(rows[0]).toHaveTextContent('orchestrator/router.py');
    expect(rows[0]).toHaveTextContent('0.91');
    expect(rows[1]).toHaveTextContent('agent/low.py');
  });

  it('renders an empty state', () => {
    render(<HotspotsSection hotspots={[]} />);
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText(/no churn hotspots/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- hotspots-section`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```tsx
// web-next/components/code-graph/hotspots-section.tsx
'use client';
import { CollapsibleSection } from './collapsible-section';
import type { Hotspot } from '@/types/api';

export function HotspotsSection({ hotspots }: { hotspots: Hotspot[] }) {
  const sorted = [...hotspots].sort((a, b) => b.score - a.score);
  return (
    <CollapsibleSection
      title="Hotspots"
      count={hotspots.length}
      testId="hotspots-section"
    >
      {hotspots.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No churn hotspots detected.
        </p>
      ) : (
        <table className="w-full text-left text-xs">
          <thead className="text-muted-foreground">
            <tr>
              <th className="py-1 pr-2 font-medium">File</th>
              <th className="py-1 pr-2 text-right font-medium">Churn</th>
              <th className="py-1 pr-2 text-right font-medium">Cx density</th>
              <th className="py-1 pr-2 text-right font-medium">Score</th>
              <th className="py-1 font-medium">Trend</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((h) => (
              <tr key={h.file} data-testid="hotspot-row" className="border-t">
                <td className="break-all py-1 pr-2 font-mono">{h.file}</td>
                <td className="py-1 pr-2 text-right tabular-nums">
                  {h.churn.toFixed(2)}
                </td>
                <td className="py-1 pr-2 text-right tabular-nums">
                  {h.complexity_density.toFixed(2)}
                </td>
                <td className="py-1 pr-2 text-right tabular-nums">
                  {h.score.toFixed(2)}
                </td>
                <td className="py-1">{h.trend}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </CollapsibleSection>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test -- hotspots-section`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web-next/components/code-graph/hotspots-section.tsx web-next/tests/hotspots-section.test.tsx
git commit -m "feat(code-graph): add HotspotsSection"
```

---

### Task 8: FileHealthSection

**Files:**
- Create: `web-next/components/code-graph/file-health-section.tsx`
- Test: `web-next/tests/file-health-section.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// web-next/tests/file-health-section.test.tsx
import { describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { FileHealthSection } from '@/components/code-graph/file-health-section';
import type { FileHealth } from '@/types/api';

const fileHealth: FileHealth[] = [
  { file: 'agent/good.py', maintainability_index: 80.5, band: 'good', crap: 2.0 },
  { file: 'orchestrator/router.py', maintainability_index: 30.1, band: 'poor', crap: 41.5 },
];

describe('FileHealthSection', () => {
  it('renders rows sorted by maintainability index ascending (worst first)', () => {
    render(<FileHealthSection fileHealth={fileHealth} />);
    fireEvent.click(screen.getByRole('button'));
    const rows = screen.getAllByTestId('file-health-row');
    expect(rows[0]).toHaveTextContent('orchestrator/router.py');
    expect(rows[0]).toHaveTextContent('poor');
    expect(rows[1]).toHaveTextContent('agent/good.py');
  });

  it('renders an empty state', () => {
    render(<FileHealthSection fileHealth={[]} />);
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText(/no file-health records/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- file-health-section`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```tsx
// web-next/components/code-graph/file-health-section.tsx
'use client';
import { CollapsibleSection } from './collapsible-section';
import { Badge } from '@/components/ui/badge';
import type { FileHealth } from '@/types/api';

const BAND_VARIANT: Record<
  FileHealth['band'],
  'secondary' | 'outline' | 'destructive'
> = {
  good: 'secondary',
  moderate: 'outline',
  poor: 'destructive',
};

export function FileHealthSection({
  fileHealth,
}: {
  fileHealth: FileHealth[];
}) {
  const sorted = [...fileHealth].sort(
    (a, b) => a.maintainability_index - b.maintainability_index,
  );
  return (
    <CollapsibleSection
      title="File health"
      count={fileHealth.length}
      testId="file-health-section"
    >
      {fileHealth.length === 0 ? (
        <p className="text-xs text-muted-foreground">No file-health records.</p>
      ) : (
        <table className="w-full text-left text-xs">
          <thead className="text-muted-foreground">
            <tr>
              <th className="py-1 pr-2 font-medium">File</th>
              <th className="py-1 pr-2 text-right font-medium">MI</th>
              <th className="py-1 pr-2 text-right font-medium">CRAP</th>
              <th className="py-1 font-medium">Band</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((f) => (
              <tr key={f.file} data-testid="file-health-row" className="border-t">
                <td className="break-all py-1 pr-2 font-mono">{f.file}</td>
                <td className="py-1 pr-2 text-right tabular-nums">
                  {f.maintainability_index.toFixed(1)}
                </td>
                <td className="py-1 pr-2 text-right tabular-nums">
                  {f.crap != null ? f.crap.toFixed(1) : '—'}
                </td>
                <td className="py-1">
                  <Badge variant={BAND_VARIANT[f.band]}>{f.band}</Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </CollapsibleSection>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test -- file-health-section`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web-next/components/code-graph/file-health-section.tsx web-next/tests/file-health-section.test.tsx
git commit -m "feat(code-graph): add FileHealthSection"
```

---

### Task 9: HealthTab container

**Files:**
- Create: `web-next/components/code-graph/health-tab.tsx`
- Test: `web-next/tests/health-tab.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// web-next/tests/health-tab.test.tsx
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { HealthTab } from '@/components/code-graph/health-tab';
import type { RepoGraphBlob } from '@/types/api';

function baseBlob(): RepoGraphBlob {
  return {
    commit_sha: 'abc',
    generated_at: '2026-06-08T00:00:00Z',
    analyser_version: 'phase13-health-0.13.0',
    areas: [],
    nodes: [],
    edges: [],
    public_symbols: [],
    cycles: [],
    dead_code: [],
    clones: [],
    hotspots: [],
    file_health: [
      { file: 'a.py', maintainability_index: 20, band: 'poor', crap: 10 },
    ],
    health: {
      score: 72,
      clone_count: 0,
      cycle_count: 0,
      dead_count: 0,
      hotspot_count: 0,
    },
  };
}

describe('HealthTab', () => {
  it('renders the scorecard and all six sections', () => {
    render(<HealthTab blob={baseBlob()} />);
    expect(screen.getByTestId('health-scorecard')).toBeInTheDocument();
    expect(screen.getByTestId('cycles-section')).toBeInTheDocument();
    expect(screen.getByTestId('dead-code-section')).toBeInTheDocument();
    expect(screen.getByTestId('clones-section')).toBeInTheDocument();
    expect(screen.getByTestId('hotspots-section')).toBeInTheDocument();
    expect(screen.getByTestId('file-health-section')).toBeInTheDocument();
    // poor-file count is derived from file_health
    expect(screen.getByTestId('count-Poor files')).toHaveTextContent('1');
  });

  it('shows a stale banner instead of the scorecard when health is null', () => {
    const blob = baseBlob();
    blob.health = null;
    render(<HealthTab blob={blob} />);
    expect(screen.getByTestId('health-stale')).toBeInTheDocument();
    expect(screen.queryByTestId('health-scorecard')).not.toBeInTheDocument();
    // sections still render
    expect(screen.getByTestId('cycles-section')).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- health-tab`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```tsx
// web-next/components/code-graph/health-tab.tsx
'use client';
import type { RepoGraphBlob } from '@/types/api';
import { HealthScorecard } from './health-scorecard';
import { CyclesSection } from './cycles-section';
import { DeadCodeSection } from './dead-code-section';
import { ClonesSection } from './clones-section';
import { HotspotsSection } from './hotspots-section';
import { FileHealthSection } from './file-health-section';

export function HealthTab({ blob }: { blob: RepoGraphBlob }) {
  const poorFileCount = blob.file_health.filter((f) => f.band === 'poor').length;
  return (
    <div data-testid="health-tab" className="space-y-4 py-4">
      {blob.health == null ? (
        <p
          role="status"
          data-testid="health-stale"
          className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-400"
        >
          This analysis predates the quality layer. Re-run a refresh to compute
          health metrics.
        </p>
      ) : (
        <HealthScorecard health={blob.health} poorFileCount={poorFileCount} />
      )}
      <CyclesSection cycles={blob.cycles} />
      <DeadCodeSection deadCode={blob.dead_code} />
      <ClonesSection clones={blob.clones} />
      <HotspotsSection hotspots={blob.hotspots} />
      <FileHealthSection fileHealth={blob.file_health} />
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test -- health-tab`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web-next/components/code-graph/health-tab.tsx web-next/tests/health-tab.test.tsx
git commit -m "feat(code-graph): add HealthTab container"
```

---

### Task 10: Wire the Health tab into the page

**Files:**
- Modify: `web-next/app/(app)/code-graph/[repoId]/page.tsx`
- Test: `web-next/tests/code-graph-page.test.tsx` (add a case)

- [ ] **Step 1: Add a failing test case**

Open `web-next/tests/code-graph-page.test.tsx` and add this test inside the top-level `describe` block (mirror the existing render/mocks already set up in that file — reuse its helper that renders the page with a `latest.blob`):

```tsx
  it('renders the Health tab trigger', async () => {
    // Uses the same render helper + mocked hooks the other cases in this
    // file use (a completed `latest.blob` is already provided there).
    renderPage();
    expect(
      await screen.findByRole('tab', { name: /health/i }),
    ).toBeInTheDocument();
  });
```

If the file has no shared `renderPage()` helper, copy the render setup from the nearest existing test in the same file verbatim into this case.

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- code-graph-page`
Expected: FAIL — no tab named "Health".

- [ ] **Step 3: Edit the page — import, type, parse, trigger, content**

In `web-next/app/(app)/code-graph/[repoId]/page.tsx`:

(a) Add the import alongside the other code-graph imports (after line 27):
```tsx
import { HealthTab } from '@/components/code-graph/health-tab';
```

(b) Widen the tab key type (replace line 42):
```tsx
type TabKey = 'map' | 'raw' | 'health';
```

(c) Replace the `activeTab` parse (line 84):
```tsx
  const activeTab: TabKey =
    tabFromUrl === 'raw'
      ? 'raw'
      : tabFromUrl === 'health'
        ? 'health'
        : 'map';
```

(d) Add the trigger inside `<TabsList>` (after line 207, the `raw` trigger):
```tsx
                <TabsTrigger value="health">Health</TabsTrigger>
```

(e) Add the tab content after the closing `</TabsContent>` of the `raw` tab (after line 326):
```tsx
              <TabsContent value="health" className="min-h-0 flex-1">
                {latest?.blob ? (
                  <HealthTab blob={latest.blob} />
                ) : (
                  <div
                    role="status"
                    className="flex h-[400px] items-center justify-center rounded-md border bg-card/40 text-sm text-muted-foreground"
                  >
                    Analysis in progress — first analysis can take a few
                    minutes.
                  </div>
                )}
              </TabsContent>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test -- code-graph-page`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web-next/app/(app)/code-graph/[repoId]/page.tsx web-next/tests/code-graph-page.test.tsx
git commit -m "feat(code-graph): add Health tab to repo graph page"
```

---

### Task 11: Per-node complexity badges in the node side-panel

**Files:**
- Modify: `web-next/components/code-graph/node-side-panel.tsx`
- Test: `web-next/tests/node-side-panel.test.tsx` (add cases)

- [ ] **Step 1: Add failing test cases**

Add to `web-next/tests/node-side-panel.test.tsx` (reuse its existing render setup / blob fixture builder; the panel needs a `function`-kind node selected). Add a node to the fixture with complexity fields and assert:

```tsx
  it('shows complexity badges for a function node', () => {
    // Render the panel with a selected function node that has complexity
    // fields populated (cyclomatic/cognitive/loc). Reuse the file's
    // existing render helper; pass a node id whose node has:
    //   kind: 'function', cyclomatic: 7, cognitive: 4, loc: 22
    renderPanelWithNode({
      id: 'agent/x.py::foo',
      kind: 'function',
      label: 'foo',
      file: 'agent/x.py',
      line_start: 1,
      line_end: 22,
      area: 'agent',
      parent: 'file:agent/x.py',
      cyclomatic: 7,
      cognitive: 4,
      loc: 22,
    });
    const badges = screen.getByTestId('node-complexity');
    expect(badges).toHaveTextContent('cyclomatic 7');
    expect(badges).toHaveTextContent('cognitive 4');
    expect(badges).toHaveTextContent('loc 22');
  });

  it('omits complexity badges for a non-function node', () => {
    renderPanelWithNode({
      id: 'file:agent/x.py',
      kind: 'file',
      label: 'x.py',
      file: 'agent/x.py',
      line_start: null,
      line_end: null,
      area: 'agent',
      parent: 'agent',
    });
    expect(screen.queryByTestId('node-complexity')).not.toBeInTheDocument();
  });
```

If the file lacks a `renderPanelWithNode` helper, write a small local one that builds a `RepoGraphBlob` containing just the given node (plus empty `edges`/`areas` and the quality-array defaults) and renders `<NodeSidePanel repoId={1} blob={blob} nodeId={node.id} />`. Note: because `useNodeCodePreview` fetches, mock it the same way the existing tests in this file already do (copy their `vi.mock('@/hooks/useNodeCodePreview', ...)` block if present).

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- node-side-panel`
Expected: FAIL — no element with testid `node-complexity`.

- [ ] **Step 3: Add the badges block**

In `web-next/components/code-graph/node-side-panel.tsx`, inside the header `<div className="min-w-0 flex-1">`, immediately after the closing `)}` of the `{node.file && ( ... )}` location block (after line 146), insert:

```tsx
          {node.kind === 'function' && node.cyclomatic != null && (
            <div
              data-testid="node-complexity"
              className="mt-1 flex flex-wrap gap-1"
            >
              <Badge variant="outline" className="text-[10px]">
                cyclomatic {node.cyclomatic}
              </Badge>
              {node.cognitive != null && (
                <Badge variant="outline" className="text-[10px]">
                  cognitive {node.cognitive}
                </Badge>
              )}
              {node.loc != null && (
                <Badge variant="outline" className="text-[10px]">
                  loc {node.loc}
                </Badge>
              )}
            </div>
          )}
```

(`Badge` is already imported at line 16; no new import needed.)

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test -- node-side-panel`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web-next/components/code-graph/node-side-panel.tsx web-next/tests/node-side-panel.test.tsx
git commit -m "feat(code-graph): show per-node complexity in side panel"
```

---

### Task 12: Full verification gate

**Files:** none (verification only)

- [ ] **Step 1: Run the full web-next test suite**

Run (from `web-next/`): `npm run test`
Expected: all tests PASS.

- [ ] **Step 2: Typecheck**

Run: `npm run typecheck`
Expected: no errors.

- [ ] **Step 3: Lint**

Run: `npm run lint`
Expected: no errors.

- [ ] **Step 4: Production build**

Run: `npm run build`
Expected: build succeeds (the page is a client component; confirm no SSR/type breakage).

- [ ] **Step 5: Manual render against the live API (optional but recommended)**

Run (from `web-next/`): `npm run dev` then open `http://localhost:3000/code-graph/170?tab=health` (point the dev server's API base at the VM `172.190.26.82:2020`, matching the existing local dev config). Confirm: scorecard shows a score + counts; each section expands; the node side-panel shows complexity badges for a function node on the Raw tab.

- [ ] **Step 6: No commit** (verification only). If any step fails, fix and re-run before the branch is considered done.

---

## Self-Review Notes

- **Spec coverage:** Health tab (Task 10) ✓; scorecard + 6 sections (Tasks 3–9) ✓; per-node complexity in side-panel (Task 11) ✓; "No findings" empty states (every section task) ✓; stale `health == null` banner (Task 9) ✓; types regen (Task 1) ✓; verification gate `tsc`/`lint`/`vitest`/`build` (Task 12) ✓.
- **Non-goals respected:** no cross-tab finding→node highlight, no graph node tinting, no backend changes.
- **Type consistency:** field names match `shared/types.py` exactly — `RepoHealth.{score,clone_count,cycle_count,dead_count,hotspot_count}`, `Hotspot.{file,churn,complexity_density,score,trend}`, `FileHealth.{file,maintainability_index,band,crap}`, `CloneGroup.{id,token_len,mode,instances,family_id}`, `CloneInstance.{node_id,file,line_start,line_end}`, `DeadCodeFinding.{kind,target,file,reason}`, `DependencyCycle.{id,kind,members,closing_edges}`, `Node.{cyclomatic,cognitive,loc}`. The poor-file count is derived (not a `RepoHealth` field) and computed in `HealthTab` from `file_health`.
- **Tasks 10 & 11 reuse existing test harness** in `code-graph-page.test.tsx` / `node-side-panel.test.tsx` rather than re-deriving mocks; the engineer is told to copy the in-file setup if no shared helper exists.
