'use client';
// Freshness banner — ADR-016 §11. Surfaces the analysis_branch + the
// commit_sha at the time of analysis + when it was generated. The
// "Refresh" control lives next door (see refresh-button.tsx) — keeping
// it as its own component lets tests render the banner in isolation.
import type { LatestRepoGraphData } from '@/types/api';

interface Props {
  latest: LatestRepoGraphData;
}

export function FreshnessBanner({ latest }: Props) {
  const branch = latest.analysis_branch;
  if (!latest.blob || !latest.commit_sha) {
    return (
      <div
        role="status"
        className="rounded-md border bg-card/40 p-3 text-xs text-muted-foreground"
      >
        No analysis yet on branch <span className="font-mono">{branch}</span>.
        First analysis can take a few minutes — click Refresh to start.
      </div>
    );
  }

  const sha7 = latest.commit_sha.slice(0, 7);
  const generated = latest.generated_at ? new Date(latest.generated_at) : null;
  const generatedLabel = generated
    ? generated.toLocaleString()
    : 'unknown time';

  const statusLabel =
    latest.status === 'partial'
      ? ' — some areas failed; expand them for the error'
      : latest.status === 'failed'
        ? ' — analysis failed for every area'
        : '';

  return (
    <div
      role="status"
      data-testid="graph-freshness-banner"
      className="rounded-md border bg-card/40 p-3 text-xs text-muted-foreground"
    >
      Graph from <span className="font-mono">{branch}</span>@
      <span className="font-mono">{sha7}</span> analysed {generatedLabel}
      {statusLabel}.
    </div>
  );
}
