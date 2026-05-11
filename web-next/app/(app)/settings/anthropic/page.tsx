'use client';
import SecretForm from '@/components/settings/secret-form';

export default function AnthropicSettingsPage() {
  return (
    <div className="p-6 max-w-2xl">
      <h1 className="text-2xl font-semibold mb-2">Anthropic API key</h1>
      <p className="text-sm text-muted-foreground mb-6">
        If you'd rather pay Anthropic directly than use the shared subscription,
        paste your API key here. Tasks you create will run against your key.
        The key is stored encrypted on the server and is never sent back to
        your browser.
      </p>
      <SecretForm
        secretKey="anthropic_api_key"
        label="Anthropic API key"
        placeholder="sk-ant-…"
        helpText="Get one at console.anthropic.com → API Keys."
      />
    </div>
  );
}
