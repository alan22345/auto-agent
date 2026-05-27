'use client';
import { QueryClientProvider } from '@tanstack/react-query';
import { queryClient } from '@/lib/queryClient';
import { useEffect } from 'react';
import { wsClient } from '@/lib/ws';
import { Toaster } from '@/components/ui/toaster';

export function Providers({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    // Skip the websocket on the standalone /map-demo verification route
    // — there's no backend to connect to and the dev-server proxy
    // accumulates failed reconnects until it wedges.
    if (
      typeof window !== 'undefined' &&
      window.location.pathname.startsWith('/map-demo')
    ) {
      return;
    }
    wsClient.connect();
    return () => wsClient.disconnect();
  }, []);
  return (
    <QueryClientProvider client={queryClient}>
      {children}
      <Toaster />
    </QueryClientProvider>
  );
}
