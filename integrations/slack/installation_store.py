"""Postgres-backed slack-bolt installation store.

We don't subclass slack_bolt's AsyncInstallationStore directly to avoid a
hard dependency on its private signatures — instead we expose the same
method names slack-bolt's AsyncApp expects (`async_save`,
`async_find_bot`, `async_delete_installation`) and pass an instance into
`AsyncApp(installation_store=...)`.

Why per-org instantiation? The OAuth callback knows which org just
installed; pass that org_id into the store at construction so save() can
stamp the FK without parsing the Slack `team_id` out into a separate
mapping table.

Lookups (async_find_bot) are global — slack-bolt only gives us the
team_id, and we resolve org_id from `slack_installations.team_id`."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text

from shared import installation_crypto
from shared.database import async_session

log = logging.getLogger(__name__)


@dataclass
class BotInstallation:
    """Subset of slack-bolt's Bot data class — what AsyncApp actually
    reads from `async_find_bot`. We only fill what's needed: bot_token,
    bot_user_id, team_id."""

    bot_token: str
    bot_user_id: str
    team_id: str
    enterprise_id: str | None = None


class PostgresInstallationStore:
    def __init__(self, org_id: int | None = None):
        # org_id is only required at install time. async_find_bot does not
        # need it (we resolve org from team_id).
        self.org_id = org_id

    async def async_save(self, installation) -> None:
        if self.org_id is None:
            raise RuntimeError(
                "PostgresInstallationStore.async_save called without org_id "
                "— construct with org_id at OAuth callback time."
            )
        async with async_session() as session:
            bot_token_enc = await installation_crypto.encrypt(
                installation.bot_token, session=session
            )
            app_token_enc = None
            if getattr(installation, "app_token", None):
                app_token_enc = await installation_crypto.encrypt(
                    installation.app_token, session=session
                )
            await session.execute(
                text(
                    """
                    INSERT INTO slack_installations
                        (org_id, team_id, team_name, bot_token_enc,
                         bot_user_id, app_token_enc,
                         installed_by_slack_user_id, installed_at)
                    VALUES
                        (:org_id, :team_id, :team_name, :bot_token_enc,
                         :bot_user_id, :app_token_enc,
                         :installed_by, now())
                    ON CONFLICT (org_id) DO UPDATE SET
                        team_id = EXCLUDED.team_id,
                        team_name = EXCLUDED.team_name,
                        bot_token_enc = EXCLUDED.bot_token_enc,
                        bot_user_id = EXCLUDED.bot_user_id,
                        app_token_enc = EXCLUDED.app_token_enc,
                        installed_by_slack_user_id = EXCLUDED.installed_by_slack_user_id,
                        installed_at = now()
                    """
                ),
                {
                    "org_id": self.org_id,
                    "team_id": installation.team_id,
                    "team_name": getattr(installation, "team_name", None),
                    "bot_token_enc": bot_token_enc,
                    "bot_user_id": installation.bot_user_id,
                    "app_token_enc": app_token_enc,
                    "installed_by": getattr(installation, "user_id", None),
                },
            )
            await session.commit()
        log.info(
            "slack_installation_saved org_id=%s team_id=%s",
            self.org_id,
            installation.team_id,
        )

    async def async_find_bot(
        self,
        *,
        enterprise_id: str | None = None,
        team_id: str | None = None,
        is_enterprise_install: bool | None = None,
    ) -> BotInstallation | None:
        # Implemented in Task B2.
        raise NotImplementedError

    async def async_delete_installation(
        self,
        *,
        enterprise_id: str | None = None,
        team_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        # Implemented in Task B3.
        raise NotImplementedError
