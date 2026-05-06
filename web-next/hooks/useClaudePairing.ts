'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { startPairing, submitPairCode } from '@/lib/claude-pairing';

interface State {
  phase:
    | 'idle'
    | 'starting'
    | 'awaiting_url'
    | 'awaiting_code'
    | 'submitting'
    | 'done'
    | 'error';
  url: string | null;
  error: string | null;
}

const URL_RE = /(https:\/\/claude\.ai\/[^\s]+)/;

export function useClaudePairing() {
  const [state, setState] = useState<State>({
    phase: 'idle',
    url: null,
    error: null,
  });
  const [pairingId, setPairingId] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const begin = useCallback(async () => {
    setState({ phase: 'starting', url: null, error: null });
    try {
      const { pairing_id } = await startPairing();
      setPairingId(pairing_id);
      setState({ phase: 'awaiting_url', url: null, error: null });
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(
        `${proto}://${location.host}/ws/claude/pair/${pairing_id}`,
      );
      wsRef.current = ws;
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === 'line') {
            const m = msg.text.match(URL_RE);
            if (m) setState((s) => ({ ...s, phase: 'awaiting_code', url: m[1] }));
          }
        } catch {}
      };
      ws.onerror = () =>
        setState((s) => ({ ...s, phase: 'error', error: 'WebSocket error' }));
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
        setState({ phase: 'error', url: null, error: msg });
      }
    },
    [pairingId],
  );

  useEffect(() => () => wsRef.current?.close(), []);

  return { state, begin, submit };
}
