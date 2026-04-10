# Auto-Agent

Autonomous AI coding agent that picks up tasks, plans, codes, reviews, and creates PRs — powered by Claude Code.

## Architecture

- **FastAPI** server on port 2020 (API + WebSocket + webhooks)
- **PostgreSQL** for tasks, repos, suggestions
- **Redis** for event streaming between services
- **Claude Code CLI** for AI-powered planning, coding, and review
- **Docker Compose** for deployment

## Quick Start (Local Dev)

```bash
cp .env.example .env   # fill in tokens
docker compose up -d   # starts postgres, redis, auto-agent
open http://localhost:2020
```

## Deploying to Azure VM

The app runs on an Azure VM (`auto-agent-vm` in `AUTO-AGENT-RG`, `172.190.26.82`).

### Deploy (no migration)

```bash
./scripts/deploy.sh
```

### Deploy with database migration

```bash
./scripts/deploy.sh migrate
```

### What the deploy script does

1. `rsync` project files to `azureuser@172.190.26.82:~/auto-agent/` (excludes `.env`, `.git`, `.venv`, `.workspaces`)
2. If `migrate` flag: rebuilds image, runs `alembic upgrade head` via one-off container
3. Rebuilds Docker image and restarts the `auto-agent` service
4. Waits for health check at `/health`

### First-time VM setup

```bash
ssh azureuser@172.190.26.82
cd ~/auto-agent
cp .env.example .env   # fill in GITHUB_TOKEN, TELEGRAM_BOT_TOKEN, etc.
./scripts/auth.sh      # authenticate Claude Code CLI
docker compose up -d
```

### Manual operations on VM

```bash
# SSH in
ssh azureuser@172.190.26.82

# Logs
cd ~/auto-agent && docker compose logs -f auto-agent

# Restart
docker compose restart auto-agent

# Run migration manually
docker compose run --rm -w /app -e PYTHONPATH=/app auto-agent alembic upgrade head

# DB shell
docker compose exec postgres psql -U autoagent -d autoagent
```

## Task Lifecycle

```
Ingested -> Classified (simple/complex) -> Queued
  -> [Complex: Planning -> Approval ->] Coding
  -> Self-Review -> Independent Review -> PR Created
  -> CI Check -> Human Review -> Done
```

## Freeform Mode

PO (Product Owner) agent periodically analyzes repos, generates improvement suggestions. Approved suggestions become tasks that auto-merge to a `dev` branch after CI passes. User promotes good changes to `main` or reverts from `dev`.

Enable via the Freeform tab in the web UI or:

```bash
curl -X POST http://localhost:2020/api/freeform/config \
  -H 'Content-Type: application/json' \
  -d '{"repo_name": "org/repo", "enabled": true, "dev_branch": "dev"}'
```
