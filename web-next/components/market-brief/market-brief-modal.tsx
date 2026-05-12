"use client";

import { useLatestMarketBrief } from "@/lib/market-brief";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function MarketBriefModal({
  repoId,
  open,
  onOpenChange,
}: {
  repoId: number | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { data: brief, isLoading } = useLatestMarketBrief(repoId, open);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Market brief</DialogTitle>
        </DialogHeader>

        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : !brief ? (
          <p className="text-sm text-muted-foreground">
            No brief yet — runs on the next PO analysis cycle.
          </p>
        ) : (
          <div className="space-y-6 text-sm">
            <section>
              <h3 className="font-semibold">Summary</h3>
              <p className="text-muted-foreground whitespace-pre-wrap">
                {brief.summary || "(empty)"}
              </p>
            </section>

            {brief.product_category && (
              <section>
                <h3 className="font-semibold">Product category</h3>
                <p>{brief.product_category}</p>
              </section>
            )}

            <BriefList
              title="Competitors"
              items={brief.competitors.map((c) => ({
                primary: c.name,
                secondary: c.why_relevant,
                sources: [c.url],
              }))}
            />
            <BriefList
              title="Findings"
              items={brief.findings.map((f) => ({
                primary: f.theme,
                secondary: f.observation,
                sources: f.sources,
              }))}
            />
            <BriefList
              title="Modality opportunities"
              items={brief.modality_gaps.map((m) => ({
                primary: m.modality,
                secondary: m.opportunity,
                sources: m.sources,
              }))}
            />
            <BriefList
              title="Strategic themes"
              items={brief.strategic_themes.map((t) => ({
                primary: t.theme,
                secondary: t.why_now,
                sources: t.sources,
              }))}
            />

            {brief.partial && (
              <p className="text-xs text-amber-600">
                Brief is partial — the researcher hit its turn cap.
              </p>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function BriefList({
  title,
  items,
}: {
  title: string;
  items: { primary: string; secondary: string; sources: string[] }[];
}) {
  if (items.length === 0) return null;
  return (
    <section>
      <h3 className="font-semibold">{title}</h3>
      <ul className="space-y-2 mt-2">
        {items.map((item, i) => (
          <li key={i} className="border-l-2 border-muted pl-3">
            <p className="font-medium">{item.primary}</p>
            <p className="text-muted-foreground">{item.secondary}</p>
            {item.sources.length > 0 && (
              <div className="mt-1 flex flex-wrap gap-2">
                {item.sources.map((url) => (
                  <a
                    key={url}
                    href={url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-blue-600 hover:underline truncate max-w-xs"
                  >
                    {url}
                  </a>
                ))}
              </div>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
