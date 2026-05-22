'use client';
// Slim layout for the verification-only /map-demo route. It only wires
// TanStack Query so ``MapCanvas``'s ``useQuery`` (LOD 3 source preview)
// works — no websocket client, no Toaster, no auth. Avoids the
// dev-server WS proxy wedge that occurs when the FastAPI backend isn't
// running.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useMemo } from 'react';

export default function MapDemoLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const qc = useMemo(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: false, staleTime: 5_000 } },
      }),
    [],
  );
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}
