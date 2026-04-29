import { describe, it, expect, vi, beforeEach } from 'vitest';
import { wsClient } from '@/lib/ws';

class FakeWS {
  static OPEN = 1;
  readyState = 0;
  onopen?: () => void;
  onmessage?: (e: { data: string }) => void;
  onclose?: () => void;
  constructor(public url: string) { setTimeout(() => { this.readyState = 1; this.onopen?.(); }, 0); }
  send = vi.fn();
  close = vi.fn();
}

beforeEach(() => {
  // @ts-expect-error stub
  global.WebSocket = FakeWS;
  Object.defineProperty(global, 'location', { value: { protocol: 'http:', host: 'x' }, configurable: true });
});

describe('wsClient', () => {
  it('dispatches events to subscribers', async () => {
    wsClient.connect();
    const handler = vi.fn();
    wsClient.subscribe(handler);
    await new Promise((r) => setTimeout(r, 5));
    // @ts-expect-error grab the socket
    wsClient['socket'].onmessage({ data: JSON.stringify({ type: 'error', message: 'x' }) });
    expect(handler).toHaveBeenCalledWith({ type: 'error', message: 'x' });
    wsClient.disconnect();
  });
});
