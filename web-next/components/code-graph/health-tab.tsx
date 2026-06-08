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
