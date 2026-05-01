'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  Brain,
  ChevronLeft,
  ChevronRight,
  ListTodo,
  LogOut,
  Search,
  Sparkles,
  type LucideIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { logout } from '@/lib/auth';
import { Button } from '@/components/ui/button';

type Tab = { href: string; label: string; icon: LucideIcon };

const tabs: Tab[] = [
  { href: '/tasks', label: 'Tasks', icon: ListTodo },
  { href: '/freeform', label: 'Freeform', icon: Sparkles },
  { href: '/memory', label: 'Memory', icon: Brain },
  { href: '/search', label: 'Search', icon: Search },
];

const STORAGE_KEY = 'auto-agent.sidebar.collapsed';

export function Sidebar({ username }: { username: string }) {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);

  // Hydrate from localStorage. We start uncollapsed to avoid a flash on
  // first paint, then sync on mount.
  useEffect(() => {
    try {
      setCollapsed(localStorage.getItem(STORAGE_KEY) === '1');
    } catch {
      // ignore — private mode etc.
    }
  }, []);

  const toggle = () => {
    setCollapsed((c) => {
      const next = !c;
      try { localStorage.setItem(STORAGE_KEY, next ? '1' : '0'); } catch {}
      return next;
    });
  };

  return (
    <aside
      className={cn(
        'flex h-screen flex-col border-r bg-card transition-[width] duration-150',
        collapsed ? 'w-14' : 'w-64',
      )}
    >
      <div
        className={cn(
          'flex items-center p-3',
          collapsed ? 'justify-center' : 'justify-between',
        )}
      >
        {!collapsed && (
          <span className="flex items-center gap-2 font-semibold">
            <span className="h-2 w-2 rounded-full bg-success" /> Auto-Agent
          </span>
        )}
        <button
          type="button"
          onClick={toggle}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          className="rounded p-1.5 text-muted-foreground hover:bg-secondary hover:text-foreground"
        >
          {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
        </button>
      </div>

      <nav className="flex flex-col gap-1 p-2">
        {tabs.map((t) => {
          const active = pathname?.startsWith(t.href);
          const Icon = t.icon;
          return (
            <Link
              key={t.href}
              href={t.href}
              title={collapsed ? t.label : undefined}
              className={cn(
                'flex items-center gap-3 rounded px-3 py-2 text-sm hover:bg-secondary',
                collapsed && 'justify-center px-2',
                active && 'bg-secondary text-primary',
              )}
            >
              <Icon size={16} className="shrink-0" />
              {!collapsed && <span>{t.label}</span>}
            </Link>
          );
        })}
      </nav>

      <div
        className={cn(
          'mt-auto border-t text-xs',
          collapsed ? 'p-2' : 'p-3',
        )}
      >
        {collapsed ? (
          <Button
            size="sm"
            variant="secondary"
            title={`Sign out (${username})`}
            className="w-full px-0"
            onClick={async () => { await logout(); location.href = '/login'; }}
          >
            <LogOut size={14} />
          </Button>
        ) : (
          <>
            <div className="mb-2 text-muted-foreground">{username}</div>
            <Button
              size="sm"
              variant="secondary"
              onClick={async () => { await logout(); location.href = '/login'; }}
            >
              Sign out
            </Button>
          </>
        )}
      </div>
    </aside>
  );
}
