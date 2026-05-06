import ConnectClaude from '@/components/settings/connect-claude';

export default function ClaudeSettingsPage() {
  return (
    <div className="p-6 max-w-2xl">
      <h1 className="text-2xl font-semibold mb-4">Connect your Claude account</h1>
      <p className="text-sm text-muted-foreground mb-6">
        Connecting your own Claude account routes your tasks to your subscription
        instead of the shared one. Optional — if you skip it, your tasks run on
        the shared admin account. Tokens stay on the server in a per-user vault
        and are never sent back to your browser.
      </p>
      <ConnectClaude />
    </div>
  );
}
