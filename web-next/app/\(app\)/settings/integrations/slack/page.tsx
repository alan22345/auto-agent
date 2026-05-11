"use client";

import { Button } from "@/components/ui/button";
import { useSlackInstall, useUninstallSlack } from "@/hooks/useIntegrations";

export default function SlackIntegrationPage() {
  const { data, isLoading } = useSlackInstall();
  const uninstall = useUninstallSlack();

  if (isLoading) return <p>Loading…</p>;

  if (!data?.connected) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-semibold">Slack</h1>
        <p className="text-muted-foreground">
          Install our bot in your Slack workspace so your team can DM tasks
          to auto-agent.
        </p>
        <a
          href="/api/integrations/slack/install"
          className="inline-block rounded-md bg-primary text-primary-foreground px-4 py-2"
        >
          Add to Slack
        </a>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Slack</h1>
      <p>
        Connected to <strong>{data.team_name ?? data.team_id}</strong>.
      </p>
      <Button
        variant="destructive"
        onClick={() => uninstall.mutate()}
        disabled={uninstall.isPending}
      >
        Disconnect
      </Button>
    </div>
  );
}
