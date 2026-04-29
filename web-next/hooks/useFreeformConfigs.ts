'use client';
import { useCallback, useEffect, useState } from 'react';
import { useWS } from './useWS';
import { wsClient } from '@/lib/ws';
import type { FreeformConfig } from '@/types/ws';

export function useFreeformConfigs() {
  const [configs, setConfigs] = useState<FreeformConfig[]>([]);

  useEffect(() => {
    wsClient.send({ type: 'load_freeform_config' });
  }, []);

  const handler = useCallback(
    (e: { type: 'freeform_config_list'; configs: FreeformConfig[] }) => {
      setConfigs(e.configs);
    },
    [],
  );

  useWS('freeform_config_list', handler);

  return configs;
}
