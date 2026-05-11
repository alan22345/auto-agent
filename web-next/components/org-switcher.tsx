'use client';
import { useRouter } from 'next/navigation';
import { useMyOrgs, useSwitchOrg } from '@/hooks/useOrgs';

export function OrgSwitcher() {
  const { data, isLoading } = useMyOrgs();
  const switchMut = useSwitchOrg();
  const router = useRouter();

  if (isLoading || !data) return null;
  if (data.orgs.length <= 1) {
    return (
      <span className="text-sm text-muted-foreground" title={`Org: ${data.current.name}`}>
        {data.current.name}
      </span>
    );
  }

  return (
    <select
      className="text-sm bg-transparent border rounded px-2 py-1"
      value={data.current.id}
      onChange={async (e) => {
        await switchMut.mutateAsync(Number(e.target.value));
        router.refresh();
      }}
      disabled={switchMut.isPending}
      aria-label="Switch organization"
    >
      {data.orgs.map((o) => (
        <option key={o.id} value={o.id}>
          {o.name}
        </option>
      ))}
    </select>
  );
}
