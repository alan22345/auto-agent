'use client';
// Freshness banner — ADR-016 §11. Surfaces the analysis_branch + the
// commit_sha at the time of analysis + when it was generated. The
// "Refresh" control lives next door (see refresh-button.tsx) — keeping
// it as its own component lets tests render the banner in isolation.
//
// Phase 7 §11 polish:
//
// * Short sha7 stays visible; full 40-char SHA surfaces in a native
//   ``title`` tooltip on hover. Same trick for the analyser version so
//   the chrome stays compact.
// * Optional ``staleness`` prop drives an amber "workspace has moved"
//   hint when ``drifted=true`` — the page polls
//   ``GET /api/repos/{id}/graph/staleness`` every 30s and feeds the
//   result in. The hint never auto-refreshes; the user must click the
//   Refresh button next door (manual-refresh discipline per the ADR).
import type { GraphStalenessResponse, LatestRepoGraphData } from '@/types/api';

interface Props {
  latest: LatestRepoGraphData;
  /** Optional drift envelope; when ``drifted=true`` the banner shows an
   * amber "workspace has moved — refresh" hint. Absent = no hint
   * (e.g. while the staleness poll is in flight or the page hasn't
   * enabled it). */
  staleness?: GraphStalenessResponse | null;
}

export function FreshnessBanner({ latest, staleness }: Props) {
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

  const fullSha = latest.commit_sha;
  const sha7 = fullSha.slice(0, 7);
  const generated = latest.generated_at ? new Date(latest.generated_at) : null;
  const generatedLabel = generated
    ? generated.toLocaleString()
    : 'unknown time';
  const analyserVersion = latest.analyser_version ?? '';

  const statusLabel =
    latest.status === 'partial'
      ? ' — some areas failed; expand them for the error'
      : latest.status === 'failed'
        ? ' — analysis failed for every area'
        : '';

  const drifted = staleness?.drifted === true;
  const workspaceSha7 = staleness?.workspace_sha
    ? staleness.workspace_sha.slice(0, 7)
    : null;

  return (
    <div
      role="status"
      data-testid="graph-freshness-banner"
      className="space-y-1 rounded-md border bg-card/40 p-3 text-xs text-muted-foreground"
    >
      <div>
        Graph from <span className="font-mono">{branch}</span>@
        <span
          className="font-mono"
          data-testid="graph-freshness-sha"
          title={fullSha}
        >
          {sha7}
        </span>{' '}
        analysed {generatedLabel}
        {analyserVersion && (
          <>
            {' '}
            ·{' '}
            <span
              className="font-mono"
              data-testid="graph-freshness-analyser-version"
              title={`Analyser ${analyserVersion}`}
            >
              {analyserVersion}
            </span>
          </>
        )}
        {statusLabel}.
      </div>
      {drifted && (
        <div
          data-testid="graph-stale-warning"
          className="rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-amber-700 dark:text-amber-300"
        >
          Workspace has moved since this graph was generated — click Refresh
          to update
          {workspaceSha7 && (
            <>
              {' '}
              (workspace is at{' '}
              <span className="font-mono" title={staleness?.workspace_sha ?? ''}>
                {workspaceSha7}
              </span>
              )
            </>
          )}
          .
        </div>
      )}
    </div>
  );
}
