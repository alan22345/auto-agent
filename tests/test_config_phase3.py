from shared.config import Settings


def test_slack_oauth_fields_exist():
    s = Settings(
        anthropic_api_key="x",
        database_url="sqlite+aiosqlite:///:memory:",
        secrets_passphrase="p",
        jwt_secret="j",
        slack_client_id="cid",
        slack_client_secret="csec",
        slack_oauth_state_secret="ssec",
        github_app_slug="auto-agent",
    )
    assert s.slack_client_id == "cid"
    assert s.slack_client_secret == "csec"
    assert s.slack_oauth_state_secret == "ssec"
    assert s.github_app_slug == "auto-agent"


def test_slack_oauth_fields_default_to_none():
    s = Settings(
        anthropic_api_key="x",
        database_url="sqlite+aiosqlite:///:memory:",
        secrets_passphrase="p",
        jwt_secret="j",
    )
    assert s.slack_client_id is None
    assert s.slack_client_secret is None
    assert s.slack_oauth_state_secret is None
