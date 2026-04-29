import { useEffect } from 'react';
import { wsClient } from '@/lib/ws';
import type { WSEvent } from '@/types/ws';

type EventOf<T extends WSEvent['type']> = Extract<WSEvent, { type: T }>;

export function useWS<T extends WSEvent['type']>(
  type: T,
  handler: (event: EventOf<T>) => void,
) {
  useEffect(() => {
    const off = wsClient.subscribe((e) => {
      if (e.type === type) handler(e as EventOf<T>);
    });
    return () => { off(); };
  }, [type, handler]);
}
