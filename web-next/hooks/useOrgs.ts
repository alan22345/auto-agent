'use client';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  fetchMembers,
  fetchMyOrgs,
  inviteMember,
  removeMember,
  switchOrg,
  type Member,
} from '@/lib/orgs';

export function useMyOrgs() {
  return useQuery({
    queryKey: ['orgs', 'me'],
    queryFn: fetchMyOrgs,
    staleTime: 30_000,
  });
}

export function useMembers(orgId: number | undefined) {
  return useQuery({
    queryKey: ['orgs', orgId, 'members'],
    queryFn: () => (orgId ? fetchMembers(orgId) : Promise.resolve({ members: [] as Member[] })),
    enabled: !!orgId,
  });
}

export function useSwitchOrg() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: switchOrg,
    onSuccess: () => {
      // Active org changed — everything tenant-scoped becomes stale.
      qc.invalidateQueries();
    },
  });
}

export function useInviteMember(orgId: number | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ email, role }: { email: string; role: 'admin' | 'member' }) => {
      if (!orgId) throw new Error('no active org');
      return inviteMember(orgId, email, role);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['orgs', orgId, 'members'] }),
  });
}

export function useRemoveMember(orgId: number | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (user_id: number) => {
      if (!orgId) throw new Error('no active org');
      return removeMember(orgId, user_id);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['orgs', orgId, 'members'] }),
  });
}
