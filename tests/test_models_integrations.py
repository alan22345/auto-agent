"""ORM smoke test — the three new Phase 3 models import, instantiate, and
expose the load-bearing column names. Schema correctness lives in the
migration; this is type-shape coverage."""
from __future__ import annotations

from shared.models import GitHubInstallation, SlackInstallation, WebhookSecret


def test_slack_installation_columns():
    cols = {c.name for c in SlackInstallation.__table__.columns}
    assert {
        "org_id", "team_id", "team_name", "bot_token_enc",
        "bot_user_id", "app_token_enc", "installed_by_slack_user_id",
        "installed_at",
    } <= cols


def test_github_installation_columns():
    cols = {c.name for c in GitHubInstallation.__table__.columns}
    assert {
        "org_id", "installation_id", "account_login", "account_type",
        "installed_at",
    } <= cols


def test_webhook_secret_columns():
    cols = {c.name for c in WebhookSecret.__table__.columns}
    assert {"org_id", "source", "secret_enc", "created_at"} <= cols


def test_slack_installation_table_name():
    assert SlackInstallation.__tablename__ == "slack_installations"


def test_github_installation_table_name():
    assert GitHubInstallation.__tablename__ == "github_installations"


def test_webhook_secret_table_name():
    assert WebhookSecret.__tablename__ == "webhook_secrets"
