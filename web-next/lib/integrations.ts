import { api } from './api';

export type SlackInstall =
  | { connected: false }
  | { connected: true; team_id: string; team_name: string | null };

export type GitHubInstall =
  | { connected: false }
  | {
      connected: true;
      installation_id: number;
      account_login: string;
      account_type: 'User' | 'Organization';
    };

export function fetchSlackInstall(): Promise<SlackInstall> {
  return api<SlackInstall>('/api/integrations/slack');
}

export function fetchGitHubInstall(): Promise<GitHubInstall> {
  return api<GitHubInstall>('/api/integrations/github');
}

export function uninstallSlack(): Promise<void> {
  return api<void>('/api/integrations/slack/uninstall', { method: 'POST' });
}

export function uninstallGitHub(): Promise<void> {
  return api<void>('/api/integrations/github/uninstall', { method: 'POST' });
}
