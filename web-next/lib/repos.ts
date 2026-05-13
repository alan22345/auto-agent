import { api } from './api';
import type { RepoData } from '@/types/api';

export async function listRepos(): Promise<RepoData[]> {
  return api<RepoData[]>('/api/repos');
}

export async function updateProductBrief(
  repoId: number,
  productBrief: string,
): Promise<RepoData> {
  return api<RepoData>(`/api/repos/${repoId}/product-brief`, {
    method: 'PATCH',
    body: JSON.stringify({ product_brief: productBrief }),
  });
}
