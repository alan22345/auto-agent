'use client';
import type { RepoGraphBlob } from '@/types/api';
import { HealthScorecard } from './health-scorecard';
import { CyclesSection } from './cycles-section';
import { DeadCodeSection } from './dead-code-section';
import { ClonesSection } from './clones-section';
import { HotspotsSection } from './hotspots-section';
import { FileHealthSection } from './file-health-section';

export function HealthTab({ blob }: { blob: RepoGraphBlob }) {
  // The quality arrays are optional on the wire — blobs produced before
  // the quality layer omit them entirely. Coalesce so the sections (and
  // the poor-file derivation) get a real array either way.
  const cycles = blob.cycles ?? [];
  const deadCode = blob.dead_code ?? [];
  const clones = blob.clones ?? [];
  const hotspots = blob.hotspots ?? [];
  const fileHealth = blob.file_health ?? [];
  const poorFileCount = fileHealth.filter((f) => f.band === 'poor').length;
  return (
    <div data-testid="health-tab" className="space-y-4 py-4">
      {blob.health == null ? (
        <p
          role="note"
          data-testid="health-stale"
          className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-400"
        >
          This analysis predates the quality layer. Re-run a refresh to compute
          health metrics.
        </p>
      ) : (
        <HealthScorecard health={blob.health} poorFileCount={poorFileCount} />
      )}
      <CyclesSection cycles={cycles} />
      <DeadCodeSection deadCode={deadCode} />
      <ClonesSection clones={clones} />
      <HotspotsSection hotspots={hotspots} />
      <FileHealthSection fileHealth={fileHealth} />
    </div>
  );
}
