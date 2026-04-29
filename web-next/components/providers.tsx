'use client';
import { QueryClientProvider } from '@tanstack/react-query';
import { queryClient } from '@/lib/queryClient';
import { useEffect } from 'react';
import { wsClient } from '@/lib/ws';
import { Toaster } from '@/components/ui/toaster';

export function Providers({ children }: { children: React.ReactNode }) {
  useEffect(() => {
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
