'use client';
import type { MemoryEntityDetail } from '@/types/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import { FactRow } from './fact-row';
import type { FactOp } from '@/hooks/useMemoryBrowser';

interface Props {
  detail: MemoryEntityDetail;
  includeSuperseded: boolean;
  setIncludeSuperseded: (v: boolean) => void;
  pendingByFact: Record<string, FactOp>;
  factErrors: Record<string, string>;
  onBack: () => void;
  onEdit: (fact_id: string, content: string) => void;
  onCorrect: (fact_id: string, content: string) => void;
  onDelete: (fact_id: string) => void;
}

export function EntityDetail({
  detail,
  includeSuperseded,
  setIncludeSuperseded,
  pendingByFact,
  factErrors,
  onBack,
  onEdit,
  onCorrect,
  onDelete,
}: Props) {
  const { entity, facts } = detail;
  return (
    <div className="flex h-full flex-col">
      <div className="mb-3 flex items-center justify-between gap-2">
        <Button variant="ghost" size="sm" onClick={onBack}>
          ← Back
        </Button>
        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          <Switch
            checked={includeSuperseded}
            onCheckedChange={(v) => setIncludeSuperseded(!!v)}
          />
          <span>Show superseded</span>
        </label>
      </div>
      <div className="mb-3">
        <div className="flex items-baseline justify-between gap-2">
          <h3 className="text-lg font-semibold">{entity.name}</h3>
          <Badge variant="outline" className="text-[10px]">
            {entity.type}
          </Badge>
        </div>
        {entity.tags && entity.tags.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {entity.tags.map((t) => (
              <Badge key={t} variant="secondary" className="text-[10px]">
                {t}
              </Badge>
            ))}
          </div>
        )}
        <p className="mt-1 text-xs text-muted-foreground">
          {facts?.length ?? 0} fact{(facts?.length ?? 0) === 1 ? '' : 's'}
          {includeSuperseded ? '' : ' (current only)'}
        </p>
      </div>
      <ul className="flex flex-col gap-2 overflow-auto">
        {(facts ?? []).map((f) => (
          <FactRow
            key={f.id}
            fact={f}
            pending={pendingByFact[f.id]}
            error={factErrors[f.id]}
            onEdit={onEdit}
            onCorrect={onCorrect}
            onDelete={onDelete}
          />
        ))}
        {(facts ?? []).length === 0 && (
          <li className="rounded border border-dashed p-4 text-center text-xs text-muted-foreground">
            No facts to show.
          </li>
        )}
      </ul>
    </div>
  );
}
