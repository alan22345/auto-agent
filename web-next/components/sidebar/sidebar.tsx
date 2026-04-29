'use client';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { cn } from '@/lib/utils';
import { logout } from '@/lib/auth';
import { Button } from '@/components/ui/button';

const tabs = [
  { href: '/tasks', label: 'Tasks' },
  { href: '/freeform', label: 'Freeform' },
  { href: '/memory', label: 'Memory' },
];

export function Sidebar({ username }: { username: string }) {
  const pathname = usePathname();
  return (
    <aside className="flex h-screen w-64 flex-col border-r bg-card">
      <div className="flex items-center justify-between p-4">
        <span className="flex items-center gap-2 font-semibold">
          <span className="h-2 w-2 rounded-full bg-success" /> Auto-Agent
        </span>
      </div>
      <nav className="flex flex-col gap-1 p-2">
        {tabs.map((t) => (
          <Link
            key={t.href}
            href={t.href}
            className={cn(
              'rounded px-3 py-2 text-sm hover:bg-secondary',
              pathname?.startsWith(t.href) && 'bg-secondary text-primary',
            )}
          >
            {t.label}
          </Link>
        ))}
      </nav>
      <div className="mt-auto border-t p-3 text-xs">
        <div className="mb-2 text-muted-foreground">{username}</div>
        <Button size="sm" variant="secondary" onClick={async () => { await logout(); location.href = '/login'; }}>
          Sign out
        </Button>
      </div>
    </aside>
  );
}
