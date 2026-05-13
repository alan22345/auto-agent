'use client';
import { Button } from '@/components/ui/button';
import { usePauseTrio } from '@/hooks/useTrioArtifacts';

export function PauseTrioButton({ taskId }: { taskId: number }) {
  const m = usePauseTrio();
  return (
    <Button
      size="sm"
      variant="outline"
      onClick={() => m.mutate(taskId)}
      disabled={m.isPending}
    >
      {m.isPending ? 'Pausing…' : 'Pause Trio'}
    </Button>
  );
}
