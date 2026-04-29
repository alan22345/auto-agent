'use client';
import { useQuery } from '@tanstack/react-query';
import { me } from '@/lib/auth';

export function useAuth() {
  return useQuery({ queryKey: ['auth', 'me'], queryFn: me, retry: false });
}
