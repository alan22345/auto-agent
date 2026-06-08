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
