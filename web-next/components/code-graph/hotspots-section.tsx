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
