import type { WSEvent, WSCommand } from '@/types/ws';

type Listener = (event: WSEvent) => void;

class WSClient {
  private socket: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private reconnectAttempts = 0;
  private explicitlyClosed = false;

  connect() {
    if (this.socket && this.socket.readyState <= 1) return;
    this.explicitlyClosed = false;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    this.socket = new WebSocket(`${proto}://${location.host}/ws`);
    this.socket.onopen = () => { this.reconnectAttempts = 0; };
    this.socket.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data) as WSEvent;
        this.listeners.forEach((l) => l(event));
      } catch {}
    };
    this.socket.onclose = () => {
      if (this.explicitlyClosed) return;
      const delay = Math.min(30_000, 1000 * 2 ** this.reconnectAttempts) + Math.random() * 500;
      this.reconnectAttempts++;
      setTimeout(() => this.connect(), delay);
    };
  }

  disconnect() {
    this.explicitlyClosed = true;
    this.socket?.close();
    this.socket = null;
  }

  send(cmd: WSCommand) {
    if (this.socket?.readyState !== WebSocket.OPEN) return false;
    this.socket.send(JSON.stringify(cmd));
    return true;
  }

  subscribe(l: Listener) {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  }
}

export const wsClient = new WSClient();
