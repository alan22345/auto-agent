from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database — set DATABASE_URL directly, or set the parts for Docker
    database_url: str = "postgresql+asyncpg://autoagent:changeme@postgres:5432/autoagent"
    postgres_user: str = "autoagent"
    postgres_password: str = "changeme"
    postgres_db: str = "autoagent"

    # Shared team-memory DB (atlas) — separate from the orchestrator's local DB.
    # Empty string disables the memory integration (recall returns "").
    team_memory_database_url: str = ""

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Internal
    orchestrator_url: str = "http://localhost:2020/api"

    # Slack
    slack_bot_token: str = ""  # xoxb-... — bot user OAuth token (chat.postMessage etc.)
    slack_app_token: str = ""  # xapp-... — app-level token for Socket Mode inbound
    slack_channel_id: str = ""  # legacy: a single shared channel (kept for back-compat)
    # Optional admin DM channel for system-scoped events (PO analyzer,
    # architect, repo onboarding) that have no task owner. Set this to your
    # Slack user ID so those events ping you only.
    slack_admin_user_id: str = ""

    # --- Phase 3: per-org Slack OAuth ---
    slack_client_id: str | None = None
    slack_client_secret: str | None = None
    slack_oauth_state_secret: str | None = None
    # GitHub App slug used to build the install URL
    # (e.g. https://github.com/apps/auto-agent/installations/new)
    github_app_slug: str | None = None

    # Linear
    linear_api_key: str = ""
    linear_team_id: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""  # Your personal chat ID with the bot

    # GitHub
    github_token: str = ""
    github_webhook_secret: str = ""
    # GitHub App credentials (optional — overrides ``github_token`` when set).
    # When configured, ``shared.github_auth.get_github_token()`` mints
    # short-lived installation tokens instead of using the PAT, so PRs and
    # commits show up as ``auto-agent[bot]`` and there's no human PAT to
    # rotate. ``github_app_private_key`` is the contents of the .pem file
    # downloaded from the GitHub App settings page (with literal ``\n``
    # line breaks preserved — Pydantic-Settings reads .env values as raw
    # strings).
    github_app_id: str = ""
    github_app_private_key: str = ""
    github_app_installation_id: str = ""
    # Default owner (user or org) for newly-created repos. Empty = look up
    # the token's user from GET /user.
    github_owner: str = ""

    # Auth — seed admin user on first boot
    admin_username: str = "admin"
    admin_password: str = ""  # Must be set for first boot
    jwt_secret: str = "auto-agent-jwt-secret-change-me"

    # LLM provider: "claude_cli" (default), "anthropic", or "bedrock"
    llm_provider: str = "claude_cli"
    llm_model: str = "claude-sonnet-4-6"
    anthropic_api_key: str = ""
    # Search tab — Brave Search API key. /search endpoints return 503 if unset.
    brave_api_key: str = ""
    # Bedrock — explicit credentials for VM deployment (optional).
    # Preferred: AWS Bedrock API key (AWS_BEARER_TOKEN_BEDROCK) — simpler, no IAM setup.
    # Alternative: IAM access keys (aws_access_key_id + aws_secret_access_key).
    # Fallback: AWS credential chain (~/.aws/credentials, SSO, instance role).
    bedrock_region: str = "us-east-1"
    aws_bearer_token_bedrock: str = ""  # Preferred: Bedrock API key
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""  # Optional, for temporary credentials

    # Concurrency: a single global pool. Per-repo cap of 1 is enforced in
    # orchestrator/queue.py separately to prevent concurrent agents on the
    # same working tree.
    max_concurrent_workers: int = 5

    # Root for per-user data. Each user's Claude credentials live at
    # f"{users_data_dir}/{user_id}/.claude/".
    users_data_dir: str = "/data/users"

    # If set, users who have not paired their own Claude credentials fall back
    # to this user_id's vault instead of being blocked. Set to the admin's
    # user_id to let teammates opt out of pairing and share the admin's
    # subscription. Leave None to require every user to pair.
    fallback_claude_user_id: int | None = None

    # Per-user encrypted secrets (Phase 1 multi-tenant). Used as the
    # passphrase for pgcrypto's pgp_sym_encrypt / pgp_sym_decrypt over the
    # ``user_secrets`` table. MUST be set in production — empty value makes
    # ``shared.secrets`` refuse to read or write so a misconfigured boot
    # never silently encrypts with an empty key.
    secrets_passphrase: str = ""

    # Public base URL used to build links in transactional emails (signup
    # verification, password reset). Points at the Next.js frontend, not the
    # FastAPI backend on 2020. Trailing slash is stripped at use time.
    app_base_url: str = "http://localhost:3000"

    # Resend (transactional email). When ``resend_api_key`` is empty the
    # email helpers log the would-be link and no-op — useful for local dev
    # so signup still completes (grab the link from the logs).
    resend_api_key: str = ""
    resend_from: str = "auto-agent <onboarding@resend.dev>"

    # Logging
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
