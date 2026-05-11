import { redirect } from 'next/navigation';
import Link from 'next/link';
import { cookies } from 'next/headers';
import { Sidebar } from '@/components/sidebar/sidebar';

interface Me {
  username: string;
  claude_auth_status?: 'paired' | 'expired' | 'never_paired';
}

async function getMe(): Promise<Me | null> {
  const cookie = (await cookies()).get('auto_agent_session')?.value;
  if (!cookie) return null;
  const res = await fetch(`${process.env.API_URL || 'http://localhost:2020'}/api/auth/me`, {
    headers: { Cookie: `auto_agent_session=${cookie}` },
    cache: 'no-store',
  });
  if (!res.ok) return null;
  return res.json();
}

export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const user = await getMe();
  if (!user) redirect('/login');
  // Only banner expired credentials. "never_paired" is a normal state when a
  // shared/fallback account is configured — silent for those users.
  const showAuthBanner = user.claude_auth_status === 'expired';
  return (
    <div className="flex h-screen">
      <Sidebar username={user.username} />
      <main className="flex-1 overflow-hidden flex flex-col">
        {showAuthBanner && (
          <div className="bg-amber-100 border-b border-amber-300 text-amber-900 px-4 py-2 text-sm flex items-center justify-between">
            <span>
              Your Claude session expired. Reconnect to resume queued tasks.
            </span>
            <Link href="/settings/claude" className="underline font-medium">
              Connect
            </Link>
          </div>
        )}
        <div className="flex-1 overflow-hidden">{children}</div>
      </main>
    </div>
  );
}
