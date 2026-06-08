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
