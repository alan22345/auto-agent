from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database — set DATABASE_URL directly, or set the parts for Docker
    database_url: str = "postgresql+asyncpg://autoagent:changeme@postgres:5432/autoagent"
    postgres_user: str = "autoagent"
    postgres_password: str = "changeme"
    postgres_db: str = "autoagent"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Internal
    orchestrator_url: str = "http://localhost:2020/api"

    # Slack
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_channel_id: str = ""

    # Linear
    linear_api_key: str = ""
    linear_team_id: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""  # Your personal chat ID with the bot

    # GitHub
    github_token: str = ""
    github_webhook_secret: str = ""
    # Default owner (user or org) for newly-created repos. Empty = look up
    # the token's user from GET /user.
    github_owner: str = ""

    # Auth — seed admin user on first boot
    admin_username: str = "admin"
    admin_password: str = ""  # Must be set for first boot
    jwt_secret: str = "auto-agent-jwt-secret-change-me"

    # LLM provider: "anthropic", "bedrock", or "claude_cli"
    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-4-6"
    anthropic_api_key: str = ""
    # Bedrock — explicit credentials for VM deployment (optional).
    # Preferred: AWS Bedrock API key (AWS_BEARER_TOKEN_BEDROCK) — simpler, no IAM setup.
    # Alternative: IAM access keys (aws_access_key_id + aws_secret_access_key).
    # Fallback: AWS credential chain (~/.aws/credentials, SSO, instance role).
    bedrock_region: str = "us-east-1"
    aws_bearer_token_bedrock: str = ""  # Preferred: Bedrock API key
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""  # Optional, for temporary credentials

    # Concurrency
    max_concurrent_simple: int = 1
    max_concurrent_complex: int = 1

    # Logging
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
