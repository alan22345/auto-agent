'use client';
import SecretForm from '@/components/settings/secret-form';

export default function GitHubSettingsPage() {
  return (
    <div className="p-6 max-w-2xl">
      <h1 className="text-2xl font-semibold mb-2">GitHub</h1>
      <p className="text-sm text-muted-foreground mb-6">
        Paste a personal access token here to have auto-agent push commits and
        open PRs as you. The token is stored encrypted on the server and is
        never sent back to your browser. Without one, your tasks fall back to
        the shared GitHub App or the org-level token.
      </p>
      <SecretForm
        secretKey="github_pat"
        label="GitHub personal access token"
        placeholder="ghp_…"
        helpText="Needs the `repo` scope at minimum. Create one at github.com/settings/tokens."
      />
    </div>
  );
}
