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

    # Web UI auth (HTTP Basic). Empty password disables auth.
    # Username is always "admin".
    web_auth_password: str = ""

    # LLM provider: "anthropic", "openai", or "claude_cli"
    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-4-20250514"
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Concurrency
    max_concurrent_simple: int = 1
    max_concurrent_complex: int = 1

    # Logging
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
