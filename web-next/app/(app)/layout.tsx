import { redirect } from 'next/navigation';
import { cookies } from 'next/headers';
import { Sidebar } from '@/components/sidebar/sidebar';

async function getMe() {
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
  return (
    <div className="flex h-screen">
      <Sidebar username={user.username} />
      <main className="flex-1 overflow-hidden">{children}</main>
    </div>
  );
}
