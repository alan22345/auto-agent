'use client';
import { useCallback, useState } from 'react';
import { startPairing, submitPairCode } from '@/lib/claude-pairing';

interface State {
  phase:
    | 'idle'
    | 'starting'
    | 'awaiting_code'
    | 'submitting'
    | 'done'
    | 'error';
  url: string | null;
  error: string | null;
}

export function useClaudePairing() {
  const [state, setState] = useState<State>({
    phase: 'idle',
    url: null,
    error: null,
  });
  const [pairingId, setPairingId] = useState<string | null>(null);

  const begin = useCallback(async () => {
    setState({ phase: 'starting', url: null, error: null });
    try {
      const { pairing_id, authorize_url } = await startPairing();
      setPairingId(pairing_id);
      setState({ phase: 'awaiting_code', url: authorize_url, error: null });
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'failed to start pairing';
      setState({ phase: 'error', url: null, error: msg });
    }
  }, []);

  const submit = useCallback(
    async (code: string) => {
      if (!pairingId) return;
      setState((s) => ({ ...s, phase: 'submitting' }));
      try {
        await submitPairCode(pairingId, code);
        setState({ phase: 'done', url: null, error: null });
      } catch (e) {
        const msg = e instanceof Error ? e.message : 'pairing failed';
        setState((s) => ({ ...s, phase: 'error', error: msg }));
      }
    },
    [pairingId],
  );

  return { state, begin, submit };
}
