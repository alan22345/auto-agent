"use client";

import { Button } from "@/components/ui/button";
import { useGitHubInstall, useUninstallGitHub } from "@/hooks/useIntegrations";

export default function GitHubIntegrationPage() {
  const { data, isLoading } = useGitHubInstall();
  const uninstall = useUninstallGitHub();

  if (isLoading) return <p>Loading…</p>;

  if (!data?.connected) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-semibold">GitHub</h1>
        <p className="text-muted-foreground">
          Install the auto-agent GitHub App on your org or user account
          to grant access to the repos you want our bot to work on.
        </p>
        <a
          href="/api/integrations/github/install"
          className="inline-block rounded-md bg-primary text-primary-foreground px-4 py-2"
        >
          Install GitHub App
        </a>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">GitHub</h1>
      <p>
        Installed on{" "}
        <strong>
          {data.account_login} ({data.account_type})
        </strong>
        .
      </p>
      <Button
        variant="destructive"
        onClick={() => uninstall.mutate()}
        disabled={uninstall.isPending}
      >
        Uninstall
      </Button>
    </div>
  );
}
