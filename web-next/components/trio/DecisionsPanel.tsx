'use client';
import { useDecisions } from '@/hooks/useTrioArtifacts';

export function DecisionsPanel({ taskId }: { taskId: number }) {
  const { data = [], isLoading } = useDecisions(taskId);
  if (isLoading) {
    return <div className="text-xs text-muted-foreground">Loading…</div>;
  }
  return (
    <div className="rounded border bg-muted/30 px-3 py-2 text-xs">
      <div className="font-medium">Decisions</div>
      {data.length === 0 ? (
        <div className="mt-1 text-muted-foreground">
          No ADRs recorded yet under <code>docs/decisions/</code>.
        </div>
      ) : (
        <ul className="mt-1 space-y-0.5">
          {data.map((d) => (
            <li key={d.filename}>
              <a
                href={d.url}
                target="_blank"
                rel="noreferrer"
                className="text-primary underline-offset-2 hover:underline"
              >
                {d.title}
              </a>
              <span className="ml-2 text-[10px] text-muted-foreground">
                {d.filename}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
