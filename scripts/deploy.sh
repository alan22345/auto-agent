#!/usr/bin/env bash
# Deploy auto-agent to the Azure VM.
#
# Usage:
#   ./scripts/deploy.sh          # deploy + restart
#   ./scripts/deploy.sh migrate  # deploy + run migration + restart
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
rsync -avz \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='.workspaces' \
  --exclude='.env' \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='.claude/settings.local.json' \
  "$SCRIPT_DIR/" "$VM:$REMOTE_DIR/"

if [[ "${1:-}" == "migrate" ]]; then
  echo "==> Running database migration..."
  ssh "$VM" "cd $REMOTE_DIR && docker compose build auto-agent && docker compose run --rm -w /app -e PYTHONPATH=/app auto-agent alembic upgrade head"
fi

echo "==> Rebuilding and restarting container..."
# Pass GITHUB_TOKEN through so the build can install the private team-memory
# package. The VM's .env GITHUB_TOKEN is reused here — `docker compose build`
# reads the build args from compose.yml, which references ${GITHUB_TOKEN}.
ssh "$VM" "cd $REMOTE_DIR && set -a && . .env && set +a && docker compose up -d --build auto-agent"

echo "==> Waiting for health check..."
sleep 6
ssh "$VM" "curl -sf http://localhost:2020/health && echo ' OK'" || echo "WARN: health check failed"

echo "==> Done. App running at http://172.190.26.82:2020"
