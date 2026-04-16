# Setup

Auto-agent is a single-user system — each person runs their own instance with their own credentials. Two people cannot share one instance (there is no per-user isolation in the database, GitHub token, LLM auth, etc).

This guide walks you through standing up your own instance. It covers the **recommended Bedrock deployment path**. See the end for alternatives (direct Anthropic API, Claude CLI pass-through).

## Prerequisites

- Docker + Docker Compose
- A GitHub account with admin access to the repos you want auto-agent to work on
- One of:
  - AWS account with Bedrock access + an inference profile for Claude Sonnet 4.6 (**recommended**)
  - Anthropic API key, or
  - Claude Max subscription (Claude Code CLI pass-through mode)
- (Optional) Telegram or Slack for notifications and remote control

## 1. Clone the repo

```bash
git clone https://github.com/<owner>/auto-agent.git
cd auto-agent
```

## 2. Create a GitHub Personal Access Token

Auto-agent uses one PAT for all git operations. PRs and commits appear under the account that owns this token.

1. Go to https://github.com/settings/tokens (Tokens classic)
2. Generate a new token with scopes: `repo`, `workflow`
3. Copy the token — you'll paste it into `.env` in step 5

## 3. Get a Bedrock API key (recommended path)

On newer AWS accounts you can mint a **Bedrock-only API key** without setting up IAM users or access keys. This is the cleanest deployment path because it works on any VM with zero AWS tooling installed.

1. Open the AWS console → **Bedrock** → **API keys** (left sidebar, under *Inference and Assessment*)
2. Create a long-lived API key scoped to your Bedrock account
3. Save the token — you'll paste it into `.env` as `AWS_BEARER_TOKEN_BEDROCK`

Make sure your account has access to the **Claude Sonnet 4.6** inference profile in `us-east-1` (or whichever region you configure). Request access from the Bedrock console → Model access.

Alternatives if Bedrock API keys aren't available:
- **IAM access keys**: set `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` in `.env` instead
- **Local dev with SSO**: leave all the AWS vars empty and run `aws sso login` locally — the SDK credential chain picks it up

## 4. (Optional) Create a Telegram bot

Skip if you don't want Telegram integration.

1. In Telegram, talk to [@BotFather](https://t.me/BotFather), run `/newbot`, follow prompts
2. Save the bot token he gives you
3. Send any message to your new bot
4. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
5. Find `"chat":{"id":<NUMBER>}` in the JSON — that's your `TELEGRAM_CHAT_ID`

The bot only accepts messages from this chat ID. Other senders are silently ignored.

## 5. Configure `.env`

```bash
cp .env.example .env   # or create from scratch with the template below
```

Minimum viable `.env`:

```bash
# Database (docker-compose provisions these)
POSTGRES_PASSWORD=<pick-a-strong-password>
DATABASE_URL=postgresql+asyncpg://autoagent:<same-password>@postgres:5432/autoagent
REDIS_URL=redis://redis:6379/0

# LLM — Bedrock (recommended)
LLM_PROVIDER=bedrock
LLM_MODEL=claude-sonnet-4-6
BEDROCK_REGION=us-east-1
AWS_BEARER_TOKEN_BEDROCK=<paste from step 3>

# GitHub
GITHUB_TOKEN=<from step 2>
# Optional: org to create new repos under (otherwise goes under your user)
GITHUB_OWNER=

# Telegram (optional)
TELEGRAM_BOT_TOKEN=<from step 4>
TELEGRAM_CHAT_ID=<from step 4>

# Web UI auth (REQUIRED if you expose this on the internet — see step 8)
WEB_AUTH_PASSWORD=

# Concurrency (tune per your Bedrock quota)
MAX_CONCURRENT_SIMPLE=1
MAX_CONCURRENT_COMPLEX=1

LOG_LEVEL=INFO
```

### Alternative LLM providers

**Direct Anthropic API:**
```bash
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-4-6
ANTHROPIC_API_KEY=sk-ant-...
```

**Claude Code CLI pass-through** (requires Claude Max subscription):
```bash
LLM_PROVIDER=claude_cli
# No model config needed — CLI uses its configured model.
# Run ./scripts/auth.sh on the host first to log Claude CLI into your Claude Max account.
```

## 6. Start it

```bash
docker compose up -d
```

This brings up postgres, redis, and the auto-agent app on port 2020.

First time only, run database migrations:

```bash
docker compose exec auto-agent alembic upgrade head
```

Check health:

```bash
curl http://localhost:2020/health
docker compose logs -f auto-agent
```

Open http://localhost:2020 — you should see the web UI.

## 7. Add a repo and send a task

1. Open the web UI → **Repos** tab → add a repo by `owner/name`
2. Type a task in the task box, or message your Telegram bot
3. Watch the task move through the lifecycle in the UI

Telegram commands: `/status`, `/done`, `/cancel`, `/delete`, `/answer`, `/branch`, `/freeform`, `/newrepo`, `/help`.

## 8. Securing the web UI (required for remote deploys)

The web UI has no per-user accounts. It's protected by HTTP Basic auth using `WEB_AUTH_PASSWORD`. Username is always `admin`.

- **Local-only** (running on your laptop, only accessing via `localhost`): leave `WEB_AUTH_PASSWORD` empty
- **Remote** (VM, cloud, anything with a public IP): **you must set `WEB_AUTH_PASSWORD`**. With it empty, anyone who finds your IP can create tasks, approve them, see your repos, and run code on your branches

GitHub/Linear webhooks (`/api/webhooks/*`) and `/health` stay publicly reachable so external services and the deploy script keep working.

Pick a strong password:

```bash
openssl rand -base64 24
```

Paste as `WEB_AUTH_PASSWORD=...` in `.env` and restart:

```bash
docker compose up -d --build auto-agent
```

## 9. Freeform mode (autonomous improvements)

The PO (Product Owner) agent periodically analyzes a repo and proposes improvement tasks. Approved suggestions run through the pipeline and auto-merge to a dev branch after CI passes. You promote good changes to main or revert from dev.

**Enable from the web UI:** Freeform tab → select repo → enable → configure cron + dev branch.

**Or via API:**

```bash
curl -X POST http://localhost:2020/api/freeform/config \
  -H 'Content-Type: application/json' \
  -d '{"repo_name": "org/repo", "enabled": true, "dev_branch": "dev", "cron": "0 */4 * * *"}'
```

## 10. Build something new from a description

Auto-agent can create a brand-new GitHub repo from a natural-language description, scaffold it, and keep improving it autonomously via freeform mode.

**Web UI:** Freeform tab → "Build something new" box → e.g. "a Next.js todo app with dark mode" → *Create & Scaffold*.

**Telegram:** `/newrepo a Next.js todo app with dark mode`

What happens:
1. Agent picks a short repo name from your description (uses `fast` model tier)
2. A private GitHub repo is created (under `GITHUB_OWNER` or your user)
3. Freeform mode is enabled and a "Scaffold" task is queued
4. The task runs through the normal pipeline but **auto-approves the plan** (independent reviewer) and **auto-merges to main** once CI passes
5. From then on, the PO analyzer proposes improvements on the configured cron

Full auto-reviewer reasoning is logged in the task timeline — you can audit what was approved and why.

## Deploying to your own VM

The provided `scripts/deploy.sh` is a template. Copy it and point at your host:

```bash
cp scripts/deploy.sh scripts/deploy-mine.sh
# edit VM= and SSH credentials to match your host, then:
./scripts/deploy-mine.sh
```

The script `rsync`s the source (excluding `.env`, `.git`, `.venv`, `.workspaces`), rebuilds the Docker image on the remote, runs migrations if needed, and restarts the container.

First-time VM setup:

```bash
ssh your-user@your-vm
git clone https://github.com/<owner>/auto-agent.git
cd auto-agent
# Copy your .env over (NEVER commit it — it has tokens)
scp .env your-user@your-vm:~/auto-agent/.env
docker compose up -d
docker compose exec auto-agent alembic upgrade head
```

Set `WEB_AUTH_PASSWORD` on any remote deployment.

## Manual operations on a running deploy

```bash
# Logs
docker compose logs -f auto-agent

# Restart
docker compose restart auto-agent

# Run a migration
docker compose exec auto-agent alembic upgrade head

# DB shell
docker compose exec postgres psql -U autoagent -d autoagent

# Redis CLI
docker compose exec redis redis-cli
```

## What's NOT shared between teammates

Two people following this guide end up with two independent instances. Tasks, repos, suggestions, freeform configs, and history live in each person's local postgres. No sync.

## Common issues

- **`gh auth` errors when creating PRs** → `GITHUB_TOKEN` is missing or lacks `repo`/`workflow` scopes
- **Telegram bot ignores messages** → wrong `TELEGRAM_CHAT_ID`. Verify via `getUpdates`
- **`[ERROR] LLM call failed: Bedrock`** → check `AWS_BEARER_TOKEN_BEDROCK` is set and you have model access in the Bedrock console
- **`503 Bedrock unable to process your request`** → transient throttling; the provider auto-retries up to 4 times with exponential backoff. If it persists, check your Bedrock quota
- **`prompt_too_long`** → the context layers (microcompact → autocompact → reactive) should handle this, but you can lower `MAX_CONCURRENT_COMPLEX` if many large tasks run in parallel
- **Agent is stuck in a read loop** → this is the Groundhog Day bug. Should be fixed by the context pipeline changes on `feature/model-agnostic-agent` branch. If you see it, file an issue with the task ID and the debug output from `eval/debug_flask.py`-style reproduction
- **Web UI loads but actions silently fail** → check `docker compose logs auto-agent` for the error
