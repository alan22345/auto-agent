'use client';
import { cn } from '@/lib/utils';

// ADR-018 — eight statuses make up the SCAFFOLD parent's state machine.
// The banner renders them as a horizontal stepper so the user can see
// where the scaffold is at a glance, even before the relevant gate card
// renders below.

interface Step {
  status: string;
  label: string;
  short: string;
}

const STEPS: Step[] = [
  { status: 'awaiting_intent_grill', label: 'Intent', short: 'A' },
  { status: 'building_root_adr', label: 'Root ADR', short: 'B' },
  { status: 'awaiting_root_adr_approval', label: 'Root review', short: 'B*' },
  { status: 'building_domain_adrs', label: 'Domain ADRs', short: 'C' },
  // ADR-018 Stage 8 — per-domain grill round runs serially inside
  // BUILDING_DOMAIN_ADRS. Park here when paused on a user question.
  { status: 'awaiting_domain_grill', label: 'Domain grill', short: 'C·' },
  { status: 'awaiting_domain_adr_approval', label: 'Domain review', short: 'C*' },
  { status: 'dispatching_domain_builds', label: 'Dispatch', short: 'D' },
  { status: 'building_domains', label: 'Building', short: 'D*' },
  { status: 'awaiting_final_verification', label: 'Verify', short: 'E' },
];

const STATUS_INDEX: Record<string, number> = Object.fromEntries(
  STEPS.map((s, i) => [s.status, i]),
);

export function ScaffoldPhaseBanner({ status }: { status: string }) {
  // A terminal status (done / blocked / failed) doesn't map to a phase
  // — we still render the banner so the user can see how far the run
  // got. If the status is unrecognised, default to -1 so nothing is
  // highlighted.
  const currentIndex = STATUS_INDEX[status] ?? -1;
  const terminal = ['done', 'blocked', 'failed'].includes(status);

  return (
    <div className="rounded border bg-muted/30 p-3">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-xs font-medium">Scaffold pipeline</div>
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
          {terminal ? status : (STEPS[currentIndex]?.label ?? status)}
        </div>
      </div>
      <ol className="flex items-center gap-1 overflow-x-auto">
        {STEPS.map((step, i) => {
          const isActive = i === currentIndex && !terminal;
          const isDone = currentIndex > i && !terminal;
          return (
            <li
              key={step.status}
              className={cn(
                'flex min-w-0 flex-1 flex-col items-center gap-1 rounded px-1 py-1 text-center text-[10px]',
                isActive && 'bg-primary/15 text-primary font-medium',
                isDone && 'text-muted-foreground',
                !isActive && !isDone && 'text-muted-foreground/60',
              )}
              title={step.status}
            >
              <span
                className={cn(
                  'flex h-5 w-5 items-center justify-center rounded-full border text-[9px] font-medium',
                  isActive && 'border-primary bg-primary text-primary-foreground',
                  isDone && 'border-muted-foreground/40 bg-muted',
                  !isActive && !isDone && 'border-muted-foreground/20',
                )}
              >
                {step.short}
              </span>
              <span className="block max-w-[6rem] truncate leading-tight">
                {step.label}
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
