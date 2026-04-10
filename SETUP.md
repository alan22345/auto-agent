# Setup

Auto-agent is a single-user system. Each person who wants to use it runs their own instance with their own credentials. Two people cannot share one instance — there is no per-user isolation in the database, GitHub token, Telegram bot, or Claude Code auth.

This guide walks you through standing up your own instance from scratch.

## Prerequisites

- Docker + Docker Compose
- A GitHub account with admin access to the repos you want auto-agent to work on
- A Claude Max subscription (auto-agent uses Claude Code under the hood)
- (Optional) A Telegram account if you want notifications and remote control

## 1. Clone the repo

```bash
git clone https://github.com/<owner>/auto-agent.git
cd auto-agent
```

## 2. Create a GitHub Personal Access Token

Auto-agent uses one PAT for all git operations. PRs and commits will appear under the account that owns this token.

1. Go to https://github.com/settings/tokens (Tokens classic)
2. Generate a new token with scopes: `repo`, `workflow`
3. Copy the token — you'll paste it into `.env` in step 5

## 3. Create a Telegram bot (optional)

Skip this section if you don't want Telegram integration.

1. In Telegram, talk to [@BotFather](https://t.me/BotFather), run `/newbot`, follow the prompts
2. Save the bot token he gives you
3. Send any message to your new bot
4. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
5. Find `"chat":{"id":<NUMBER>}` in the JSON — that's your `TELEGRAM_CHAT_ID`

The bot will only accept messages from this single chat ID. Anyone else who messages it will be silently ignored.

## 4. Authenticate Claude Code

Auto-agent runs Claude Code inside Docker, but it reads your auth from `~/.claude/` on the host. Authenticate once on the machine where you'll run docker-compose:

```bash
./scripts/auth.sh
```

Follow the prompts to log in with your Claude Max account. The auth tokens land in `~/.claude/`, which the container bind-mounts at runtime.

## 5. Configure `.env`

```bash
cp .env.example .env
```

Fill in at least:

```bash
GITHUB_TOKEN=<from step 2>

# Optional but recommended
TELEGRAM_BOT_TOKEN=<from step 3>
TELEGRAM_CHAT_ID=<from step 3>

# REQUIRED if you'll expose this on the internet — see step 7
WEB_AUTH_PASSWORD=<a strong random password>
```

`DATABASE_URL` and `REDIS_URL` already point at the postgres and redis services that docker-compose will start — leave them alone unless you're using external instances.

## 6. Start it

```bash
docker compose up -d
```

This brings up postgres, redis, and the auto-agent app on port 2020.

First time only, run database migrations:

```bash
docker compose run --rm -w /app -e PYTHONPATH=/app auto-agent alembic upgrade head
```

Open http://localhost:2020 — you should see the web UI.

## 7. Securing the web UI

The web UI has no built-in user accounts. It's protected by HTTP Basic auth using the `WEB_AUTH_PASSWORD` you set in `.env`. The username is always `admin`.

- **Local-only deployment** (running on your laptop, only accessing via `localhost`): you can leave `WEB_AUTH_PASSWORD` empty.
- **Remote deployment** (VM, cloud, anything reachable from the internet): you **must** set `WEB_AUTH_PASSWORD`. With it empty, anyone who finds your IP can create tasks, approve them, see your repos, and run code on your branches.

GitHub/Linear webhooks (`/api/webhooks/*`) and the `/health` endpoint stay publicly reachable so external services and the deploy script keep working.

To pick a strong password:

```bash
openssl rand -base64 24
```

Paste it as `WEB_AUTH_PASSWORD=...` in `.env` and restart:

```bash
docker compose up -d --build auto-agent
```

## 8. Add your first repo

In the web UI, open the **Repos** tab and add a GitHub repo by `owner/name`. Auto-agent will clone it on demand when it picks up a task.

## 9. Send a task

Either type a description in the web UI's task box, or message your Telegram bot. The bot interprets:

- Plain text → creates a task
- `/status`, `/done`, `/cancel`, `/delete`, `/answer`, `/branch`, `/freeform`, `/help` → commands

## Running on a VM instead of your laptop

If you want auto-agent to keep running when your laptop sleeps, deploy it to a VM you own. The provided `scripts/deploy.sh` is hardcoded to one specific Azure VM — copy it and change the `VM=` line to your own host:

```bash
cp scripts/deploy.sh scripts/deploy-mine.sh
# edit VM= to point at your host, then:
./scripts/deploy-mine.sh
```

The script `rsync`s the source tree, rebuilds the Docker image on the remote, and restarts the container. Your `.env` is excluded from the sync — set it up once on the remote with `scp .env <host>:~/auto-agent/.env`.

Make sure `WEB_AUTH_PASSWORD` is set on any remote deployment.

## What's NOT shared between teammates

If two people each follow this guide, they end up with two completely independent instances. Tasks, repos, suggestions, freeform configs, and history live in each person's local postgres. There is no sync between instances.

## Common issues

- **`gh auth` errors** when creating PRs → your `GITHUB_TOKEN` is missing or lacks the `repo`/`workflow` scopes.
- **Telegram bot ignores messages** → wrong `TELEGRAM_CHAT_ID`. Verify it via `getUpdates`.
- **Claude Code asks for login on every run** → `~/.claude` isn't being mounted into the container. Check `docker-compose.yml` line 49 and that `./scripts/auth.sh` ran successfully on the host.
- **Web UI loads but actions silently fail** → check `docker compose logs auto-agent` for the error.
