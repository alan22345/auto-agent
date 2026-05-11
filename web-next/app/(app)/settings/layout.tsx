import Link from 'next/link';

const NAV_ITEMS = [
  { href: '/settings/organization', label: 'Organization' },
  { href: '/settings/integrations', label: 'Integrations' },
  { href: '/settings/claude', label: 'Claude' },
  { href: '/settings/github', label: 'GitHub' },
  { href: '/settings/anthropic', label: 'Anthropic' },
];

export default function SettingsLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full">
      <nav className="w-48 border-r p-4 text-sm">
        <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Settings
        </h2>
        <ul className="space-y-1">
          {NAV_ITEMS.map((item) => (
            <li key={item.href}>
              <Link
                href={item.href}
                className="block rounded px-2 py-1 hover:bg-muted"
              >
                {item.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
      <div className="flex-1 overflow-auto">{children}</div>
    </div>
  );
}
