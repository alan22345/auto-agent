# tests/test_github_auth_per_org_app.py
"""When an org has a github_installations row, get_github_token mints
against that installation_id — not the global env-driven one."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from shared import github_auth


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    github_auth.reset_cache()
    monkeypatch.setattr(github_auth.settings, "github_app_id", "111")
    monkeypatch.setattr(
        github_auth.settings, "github_app_private_key", "-----dummy-----"
    )
    monkeypatch.setattr(
        github_auth.settings, "github_app_installation_id", "999"
    )


@pytest.mark.asyncio
async def test_per_org_installation_id_used_when_present():
    async def fake_lookup(*, org_id):
        return 42 if org_id == 7 else None

    async def fake_mint(installation_id):
        return github_auth._CachedToken(
            value=f"ghs_for_{installation_id}", expires_at=1e12
        )

    with patch(
        "shared.github_auth._installation_id_for_org",
        new=fake_lookup, create=True,
    ), patch(
        "shared.github_auth._mint_installation_token_for",
        new=fake_mint, create=True,
    ):
        tok = await github_auth.get_github_token(organization_id=7)

    assert tok == "ghs_for_42"


@pytest.mark.asyncio
async def test_falls_back_to_env_when_no_per_org_row():
    async def fake_lookup(*, org_id):
        return None

    async def fake_mint(installation_id):
        return github_auth._CachedToken(
            value=f"ghs_env_{installation_id}", expires_at=1e12
        )

    with patch(
        "shared.github_auth._installation_id_for_org",
        new=fake_lookup, create=True,
    ), patch(
        "shared.github_auth._mint_installation_token_for",
        new=fake_mint, create=True,
    ):
        tok = await github_auth.get_github_token(organization_id=7)

    assert tok == "ghs_env_999"
