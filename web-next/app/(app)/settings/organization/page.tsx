'use client';
import { useState } from 'react';
import { useInviteMember, useMembers, useMyOrgs, useRemoveMember } from '@/hooks/useOrgs';
import { Button } from '@/components/ui/button';

export default function OrganizationSettingsPage() {
  const { data: orgs, isLoading } = useMyOrgs();
  const current = orgs?.current;
  const { data: membersData, isLoading: membersLoading } = useMembers(current?.id);
  const invite = useInviteMember(current?.id);
  const remove = useRemoveMember(current?.id);

  const [email, setEmail] = useState('');
  const [role, setRole] = useState<'admin' | 'member'>('member');

  if (isLoading) {
    return <div className="p-6 text-sm text-muted-foreground">Loading…</div>;
  }
  if (!current) {
    return <div className="p-6 text-sm text-red-600">No active organization.</div>;
  }

  const canManage = current.role === 'owner' || current.role === 'admin';

  return (
    <div className="p-6 max-w-3xl space-y-8">
      <header>
        <h1 className="text-2xl font-semibold">{current.name}</h1>
        <p className="text-sm text-muted-foreground">
          slug: <code className="font-mono">{current.slug}</code> · you are <strong>{current.role}</strong>
        </p>
      </header>

      <section>
        <h2 className="text-lg font-medium mb-3">Members</h2>
        {membersLoading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-muted-foreground border-b">
              <tr>
                <th className="py-2 pr-4">Name</th>
                <th className="py-2 pr-4">Email</th>
                <th className="py-2 pr-4">Role</th>
                <th className="py-2"></th>
              </tr>
            </thead>
            <tbody>
              {(membersData?.members ?? []).map((m) => (
                <tr key={m.id} className="border-b last:border-0">
                  <td className="py-2 pr-4">{m.display_name}</td>
                  <td className="py-2 pr-4 text-muted-foreground">{m.email ?? '—'}</td>
                  <td className="py-2 pr-4">{m.role}</td>
                  <td className="py-2 text-right">
                    {canManage && m.role !== 'owner' && (
                      <button
                        className="text-red-600 hover:underline text-xs"
                        onClick={() => remove.mutate(m.id)}
                        disabled={remove.isPending}
                      >
                        Remove
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {canManage && (
        <section className="border-t pt-6">
          <h2 className="text-lg font-medium mb-3">Invite member</h2>
          <p className="text-sm text-muted-foreground mb-4">
            The invitee must already have an auto-agent account with a verified email.
          </p>
          <form
            className="flex flex-wrap gap-3 items-end"
            onSubmit={(e) => {
              e.preventDefault();
              invite.mutate(
                { email, role },
                {
                  onSuccess: () => setEmail(''),
                },
              );
            }}
          >
            <label className="flex flex-col text-sm">
              <span className="text-xs text-muted-foreground mb-1">Email</span>
              <input
                type="email"
                required
                className="border rounded px-2 py-1 w-64"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </label>
            <label className="flex flex-col text-sm">
              <span className="text-xs text-muted-foreground mb-1">Role</span>
              <select
                className="border rounded px-2 py-1"
                value={role}
                onChange={(e) => setRole(e.target.value as 'admin' | 'member')}
              >
                <option value="member">Member</option>
                <option value="admin">Admin</option>
              </select>
            </label>
            <Button type="submit" disabled={invite.isPending}>
              {invite.isPending ? 'Inviting…' : 'Invite'}
            </Button>
          </form>
          {invite.error && (
            <p className="text-red-600 text-sm mt-2">{String((invite.error as Error).message)}</p>
          )}
        </section>
      )}
    </div>
  );
}
