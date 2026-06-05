#!/usr/bin/env bash
# Deploy auto-agent to the Azure VM.
#
# Usage:
#   ./scripts/deploy.sh   # rsync, build, run migrations, restart
#
# Migrations always run — skipping them once already silently bypassed the
# design-approval gate by leaving stale .auto-agent/ artefacts in workspace
# dirs that a later task with the same id reused. Make the foot-gun
# impossible: every deploy is migrate+restart.
#
# Prerequisites:
#   - SSH access to azureuser@172.190.26.82 (auto-agent-vm in AUTO-AGENT-RG)
#   - Docker + Docker Compose on the VM
#   - First-time: run ./scripts/auth.sh on the VM to authenticate Claude Code

set -euo pipefail

VM="azureuser@172.190.26.82"
REMOTE_DIR="~/auto-agent"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Syncing files to VM..."
# --delete mirrors the working tree onto the VM so files removed/renamed in the
# repo (e.g. retired modules, renamed ADRs, orphaned web-next components) don't
# linger and break the build. Without it, stale files accumulate forever and a
# leftover component importing a since-removed hook fails `npm run build`.
# The --exclude'd paths are protected from deletion too (rsync never deletes
# excluded files), so VM-local state — secrets, the venv, build caches, agent
# workspaces, MCP config, env backups — is preserved.
rsync -avz --delete \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='.workspaces' \
  --exclude='.env' \
  --exclude='.env.bak*' \
  --exclude='.mcp.json' \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='.next' \
  --exclude='.claude/settings.local.json' \
  --exclude='.claude/worktrees' \
  --exclude='.claude/scheduled_tasks.lock' \
  "$SCRIPT_DIR/" "$VM:$REMOTE_DIR/"

echo "==> Building image + running migrations..."
# Pass GITHUB_TOKEN through so the build can install the private team-memory
# package. Build before migrate so the migration step uses an image that
# contains the new migration files. The final `up -d --build` below is a
# no-op rebuild for the auto-agent container (image already cached) but is
# still needed to bring web-next up.
ssh "$VM" "cd $REMOTE_DIR && set -a && . .env && set +a && docker compose build auto-agent && docker compose run --rm -w /app -e PYTHONPATH=/app auto-agent alembic upgrade head"

echo "==> Restarting containers..."
ssh "$VM" "cd $REMOTE_DIR && set -a && . .env && set +a && docker compose up -d --build auto-agent web-next"

echo "==> Waiting for health check..."
sleep 6
ssh "$VM" "curl -sf http://localhost:2020/health && echo ' OK'" || echo "WARN: health check failed"

echo "==> Done. Legacy UI at http://172.190.26.82:2020 — new Next.js UI at http://172.190.26.82:3000"
