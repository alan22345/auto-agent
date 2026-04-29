// Cron schedule presets — mirrors the CRON_PRESETS constant in web/static/index.html ~line 1124
export const CRON_PRESETS = [
  { label: 'Every 10 minutes', value: '*/10 * * * *' },
  { label: 'Every 30 minutes', value: '*/30 * * * *' },
  { label: 'Every hour', value: '0 * * * *' },
  { label: 'Every 4 hours', value: '0 */4 * * *' },
  { label: 'Daily at 9am', value: '0 9 * * *' },
  { label: 'Weekly (Mon 9am)', value: '0 9 * * 1' },
];
