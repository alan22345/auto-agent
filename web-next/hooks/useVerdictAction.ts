import { useState } from 'react';
import { ApiError } from '@/lib/api';
import type { ScaffoldVerdict } from '@/lib/tasks';

// Shared scaffold-ADR verdict flow used by both the root-ADR review card and
// each per-domain ADR row (ADR-018 review gates). It owns the ``submitting``
// and ``localError`` UI state and the submit lifecycle: guard re-entry,
// require a comment for ``revise``/``rejected``, run the mutation, and surface
// a friendly error. ``runVerdict`` resolves ``true`` only when the mutation
// succeeded, so callers can run their own post-success cleanup (reset the
// comment box, collapse panels, …).
export function useVerdictAction() {
  const [submitting, setSubmitting] = useState<ScaffoldVerdict | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  async function runVerdict(
    verdict: ScaffoldVerdict,
    comments: string,
    submit: () => Promise<unknown>,
    onMissingComment?: () => void,
  ): Promise<boolean> {
    if (submitting) return false;
    if ((verdict === 'revise' || verdict === 'rejected') && !comments.trim()) {
      onMissingComment?.();
      setLocalError(`Add a comment explaining why you ${verdict} this ADR.`);
      return false;
    }
    setSubmitting(verdict);
    setLocalError(null);
    try {
      await submit();
      return true;
    } catch (e) {
      setLocalError(
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : 'Failed to record verdict',
      );
      return false;
    } finally {
      setSubmitting(null);
    }
  }

  return { submitting, localError, runVerdict };
}
