'use client';

import Link from 'next/link';
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';

const INTEGRATIONS = [
  {
    href: '/settings/integrations/slack',
    title: 'Slack',
    description: 'Install our bot in your workspace.',
  },
  {
    href: '/settings/integrations/github',
    title: 'GitHub',
    description: 'Grant repo access via our GitHub App.',
  },
];

export default function IntegrationsHub() {
  return (
    <div className="p-6 max-w-3xl space-y-8">
      <header>
        <h1 className="text-2xl font-semibold">Integrations</h1>
        <p className="text-sm text-muted-foreground mt-2">
          Connect your organisation to Slack, GitHub, and other services.
        </p>
      </header>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {INTEGRATIONS.map((integration) => (
          <Link
            key={integration.href}
            href={integration.href}
            className="block transition-all hover:shadow-md"
          >
            <Card className="h-full cursor-pointer hover:bg-accent">
              <CardHeader>
                <CardTitle className="text-lg">{integration.title}</CardTitle>
                <CardDescription>{integration.description}</CardDescription>
              </CardHeader>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}
