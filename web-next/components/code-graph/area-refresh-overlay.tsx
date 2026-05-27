'use client';
// Per-area refresh affordance (ADR-016 §10 — Phase 7).
//
// Renders one small "Refresh" badge per area in the blob, positioned in
// the top-right corner of the corresponding compound area node by
// reading ``cy.getElementById(...).renderedBoundingBox()``.
//
// Click → POST /api/repos/{repoId}/graph/refresh?area=<name>. While the
// per-area refresh is in flight the badge swaps in a spinner; we also
// invalidate the latest-graph query so the page picks up the merged
// blob once the analyser publishes REPO_GRAPH_READY.
//
// The overlay deliberately lives in its own component (rather than
// inside ``graph-canvas.tsx``) so the cytoscape instance + its lifecycle
// stay one concern, and DOM-positioned controls + react-query stay
// another. The canvas exposes a ``cy`` ref via ``onCyReady`` callback.

import { useCallback, useEffect, useMemo, useState } from 'react';
import type cytoscape from 'cytoscape';
import { Loader2, RefreshCw } from 'lucide-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { refreshRepoGraph } from '@/lib/code-graph';
import { ApiError } from '@/lib/api';
import { codeGraphKeys } from '@/hooks/useCodeGraphConfigs';
import type { RepoGraphBlob } from '@/types/api';

interface Props {
  repoId: number;
  blob: RepoGraphBlob;
  cy: cytoscape.Core | null;
  /** Bumped by ``graph-canvas.tsx`` whenever the cy viewport pans/zooms
   * or the layout settles, so the overlay reflows its absolute
   * coordinates. */
  layoutTick: number;
}

interface BadgePos {
  area: string;
  // Pixel coordinates inside the cytoscape container. ``null`` while the
  // node is hidden (collapsed inside another compound, off-screen, etc.).
  left: number | null;
  top: number | null;
}

export function AreaRefreshOverlay({ repoId, blob, cy, layoutTick }: Props) {
  const areaNames = useMemo(
    () =>
      blob.nodes
        .filter((n) => n.kind === 'area')
        .map((n) => n.area),
    [blob.nodes],
  );

  const [positions, setPositions] = useState<BadgePos[]>([]);
  const [feedback, setFeedback] = useState<Record<string, string>>({});
  const qc = useQueryClient();

  const computePositions = useCallback(() => {
    if (!cy) {
      setPositions(areaNames.map((area) => ({ area, left: null, top: null })));
      return;
    }
    const next: BadgePos[] = [];
    for (const area of areaNames) {
      const ele = cy.getElementById(`area:${area}`);
      if (ele.empty() || !ele.visible()) {
        next.push({ area, left: null, top: null });
        continue;
      }
      const bb = ele.renderedBoundingBox();
      // Anchor in the top-right corner with a small inset. The container
      // is relatively-positioned in graph-canvas, so these coordinates
      // are page-local within it.
      next.push({ area, left: bb.x2 - 28, top: bb.y1 + 6 });
    }
    setPositions(next);
  }, [cy, areaNames]);

  useEffect(() => {
    computePositions();
    if (!cy) return;
    const handler = () => computePositions();
    cy.on('pan zoom resize position layoutstop', handler);
    return () => {
      cy.off('pan zoom resize position layoutstop', handler);
    };
  }, [cy, computePositions, layoutTick]);

  const mutation = useMutation({
    mutationFn: (area: string) => refreshRepoGraph(repoId, { area }),
  });

  const handleClick = useCallback(
    (area: string) => {
      mutation.mutate(area, {
        onSuccess: (resp) => {
          setFeedback((prev) => ({
            ...prev,
            [area]: `Queued ${resp.request_id.slice(0, 8)}`,
          }));
          qc.invalidateQueries({
            queryKey: [...codeGraphKeys.config(repoId), 'latest'],
          });
        },
        onError: (err: unknown) => {
          const detail =
            err instanceof ApiError
              ? err.detail
              : err instanceof Error
                ? err.message
                : 'Refresh failed';
          setFeedback((prev) => ({ ...prev, [area]: detail }));
        },
      });
    },
    [mutation, qc, repoId],
  );

  // Track in-flight area separately from the mutation object itself so
  // each badge only spins for its own request. ``mutation.isPending``
  // is true for whichever area was most recently fired; we pair that
  // with the captured ``variables`` to derive per-badge state.
  const pendingArea =
    mutation.isPending && typeof mutation.variables === 'string'
      ? (mutation.variables as string)
      : null;

  return (
    <div
      aria-hidden={false}
      // z-index above 999: cytoscape-expand-collapse mounts its
      // cue-icon canvas inside the cytoscape host at z-index 999,
      // which would otherwise intercept clicks on every refresh
      // badge in this overlay.
      className="pointer-events-none absolute inset-0 z-[1000]"
      data-testid="area-refresh-overlay"
    >
      {positions.map((p) => {
        if (p.left === null || p.top === null) {
          // Render a hidden button anyway so tests can still click it
          // when cytoscape didn't mount (e.g. jsdom). The on-screen
          // visual lives in the cytoscape-mounted branch above.
          return (
            <button
              key={p.area}
              type="button"
              data-testid={`area-refresh-${p.area}`}
              data-area={p.area}
              aria-label={`Refresh area ${p.area}`}
              onClick={() => handleClick(p.area)}
              className="pointer-events-auto sr-only"
            >
              Refresh {p.area}
            </button>
          );
        }
        const isPending = pendingArea === p.area;
        return (
          <button
            key={p.area}
            type="button"
            data-testid={`area-refresh-${p.area}`}
            data-area={p.area}
            aria-label={`Refresh area ${p.area}`}
            title={feedback[p.area] ?? `Refresh area ${p.area}`}
            onClick={() => handleClick(p.area)}
            disabled={isPending}
            className="pointer-events-auto absolute flex h-5 w-5 items-center justify-center rounded-full border border-blue-700 bg-white text-blue-700 shadow-sm transition-colors hover:bg-blue-50 disabled:cursor-wait disabled:opacity-70"
            style={{ left: p.left, top: p.top }}
          >
            {isPending ? (
              <Loader2
                size={11}
                className="animate-spin"
                data-testid={`area-refresh-spinner-${p.area}`}
              />
            ) : (
              <RefreshCw size={11} />
            )}
          </button>
        );
      })}
    </div>
  );
}
