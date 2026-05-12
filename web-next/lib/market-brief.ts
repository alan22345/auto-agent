import { useQuery } from "@tanstack/react-query";

export type MarketBrief = {
  id: number;
  repo_id: number;
  created_at: string;
  product_category: string | null;
  competitors: Array<{ name: string; url: string; why_relevant: string }>;
  findings: Array<{ theme: string; observation: string; sources: string[] }>;
  modality_gaps: Array<{ modality: string; opportunity: string; sources: string[] }>;
  strategic_themes: Array<{ theme: string; why_now: string; sources: string[] }>;
  summary: string;
  partial: boolean;
};

export function useLatestMarketBrief(repoId: number | null, enabled = true) {
  return useQuery({
    queryKey: ["market-brief", repoId],
    enabled: enabled && repoId != null && Number.isFinite(repoId),
    queryFn: async (): Promise<MarketBrief | null> => {
      const resp = await fetch(`/api/repos/${repoId}/market-brief/latest`);
      if (resp.status === 404) return null;
      if (!resp.ok) throw new Error(`brief fetch failed: ${resp.status}`);
      return resp.json();
    },
  });
}
