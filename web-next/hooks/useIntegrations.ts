'use client';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  fetchGitHubInstall,
  fetchSlackInstall,
  uninstallGitHub,
  uninstallSlack,
} from '@/lib/integrations';

export function useSlackInstall() {
  return useQuery({
    queryKey: ['integrations', 'slack'],
    queryFn: fetchSlackInstall,
  });
}

export function useGitHubInstall() {
  return useQuery({
    queryKey: ['integrations', 'github'],
    queryFn: fetchGitHubInstall,
  });
}

export function useUninstallSlack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: uninstallSlack,
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['integrations', 'slack'] }),
  });
}

export function useUninstallGitHub() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: uninstallGitHub,
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['integrations', 'github'] }),
  });
}
